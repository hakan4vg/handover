use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;
use tokio::time::timeout;

pub const BRIDGE_ADDR: &str = "127.0.0.1:38217";
/// Bound on concurrent bridge connections. The bridge owns no transfer state,
/// but an unbounded accept loop lets unauthenticated local callers pile up
/// handler work behind a slow request (F01).
const MAX_CONCURRENT_REQUESTS: usize = 16;
/// Per-request read deadline: no caller may hold a connection open
/// indefinitely before sending a complete request (F01).
const READ_TIMEOUT: Duration = Duration::from_secs(10);

pub struct Request {
    pub method: String,
    pub path: String,
    pub body: Vec<u8>,
    pub host: Option<String>,
    pub origin: Option<String>,
    pub content_type: Option<String>,
}

pub struct Response {
    status: u16,
    body: Vec<u8>,
    allow_origin: Option<String>,
}

impl Response {
    pub fn new(status: u16, body: Vec<u8>) -> Self {
        Self { status, body, allow_origin: None }
    }
    /// Reflect a validated `chrome-extension://` origin so the extension can
    /// read the response. Unvalidated origins are never reflected (F01).
    pub fn with_origin(mut self, origin: Option<String>) -> Self {
        self.allow_origin = origin;
        self
    }
}

pub async fn serve<F, Fut>(listener: TcpListener, handler: F) -> std::io::Result<()>
where
    F: Fn(Request) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Response> + Send + 'static,
{
    let handler = Arc::new(handler);
    let admissions = Arc::new(Semaphore::new(MAX_CONCURRENT_REQUESTS));
    loop {
        let (stream, _) = listener.accept().await?;
        let handler = Arc::clone(&handler);
        let permit = match admissions.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                let response = Response::new(503, error_response("bridge is busy".into()));
                tokio::spawn(async move {
                    let _ = write_response(stream, response).await;
                });
                continue;
            }
        };
        tokio::spawn(async move {
            let _permit = permit;
            let response = match read_request(stream).await {
                Ok((stream, request)) => {
                    let response = handler(request).await;
                    write_response(stream, response).await
                }
                Err((mut stream, error)) => {
                    let response = Response::new(400, error_response(error));
                    write_response(&mut stream, response).await
                }
            };
            if let Err(error) = response {
                if error.kind() == std::io::ErrorKind::BrokenPipe {
                    return;
                }
            }
        });
    }
}

async fn read_request(mut stream: TcpStream) -> Result<(TcpStream, Request), (TcpStream, String)> {
    const HEADER_LIMIT: usize = 32 * 1024;
    const BODY_LIMIT: usize = 1024 * 1024;
    let separator = b"\r\n\r\n";
    let mut bytes = Vec::with_capacity(4096);
    let header_outcome = timeout(READ_TIMEOUT, async {
        loop {
            let mut chunk = [0u8; 8192];
            let read = match stream.read(&mut chunk).await {
                Ok(read) => read,
                Err(error) => return Err(error.to_string()),
            };
            if read == 0 {
                return Err("request ended before headers were complete".to_string());
            }
            bytes.extend_from_slice(&chunk[..read]);
            if let Some(index) = bytes
                .windows(separator.len())
                .position(|window| window == separator)
            {
                break Ok(index + separator.len());
            }
            if bytes.len() > HEADER_LIMIT {
                return Err("request headers are too large".to_string());
            }
        }
    })
    .await;
    let header_end = match header_outcome {
        Ok(Ok(end)) => end,
        Ok(Err(error)) => return Err((stream, error)),
        Err(_) => return Err((stream, "request headers timed out".to_string())),
    };

    let headers = match String::from_utf8(bytes[..header_end].to_vec()) {
        Ok(headers) => headers,
        Err(_) => return Err((stream, "request headers are not utf-8".into())),
    };
    let mut lines = headers.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut request_parts = request_line.split_whitespace();
    let method = request_parts.next().unwrap_or_default().to_string();
    let target = request_parts.next().unwrap_or_default();
    if method.is_empty() || target.is_empty() {
        return Err((stream, "invalid request line".into()));
    }
    let mut fields: Vec<(String, String)> = Vec::new();
    for line in lines {
        let Some((name, value)) = line.split_once(':') else { continue };
        fields.push((name.trim().to_string(), value.trim().to_string()));
    }
    let field = |name: &str| {
        fields
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.clone())
    };
    let content_length = field("content-length")
        .map(|value| {
            value
                .parse::<usize>()
                .map_err(|_| "invalid content length".to_string())
        })
        .transpose();
    let content_length = match content_length {
        Ok(content_length) => content_length.unwrap_or(0),
        Err(error) => return Err((stream, error)),
    };
    if content_length > BODY_LIMIT {
        return Err((stream, "request body is too large".into()));
    }
    let expected = header_end + content_length;
    let body_result = timeout(READ_TIMEOUT, async {
        while bytes.len() < expected {
            let mut chunk = [0u8; 8192];
            let read = match stream.read(&mut chunk).await {
                Ok(read) => read,
                Err(error) => return Err(error.to_string()),
            };
            if read == 0 {
                return Err("request ended before the body was complete".to_string());
            }
            bytes.extend_from_slice(&chunk[..read]);
        }
        Ok(())
    })
    .await;
    match body_result {
        Ok(Ok(())) => {}
        Ok(Err(error)) => return Err((stream, error)),
        Err(_) => return Err((stream, "request body timed out".to_string())),
    }
    let path = target.split('?').next().unwrap_or(target).to_string();
    Ok((
        stream,
        Request {
            method,
            path,
            body: bytes[header_end..expected].to_vec(),
            host: field("host"),
            origin: field("origin"),
            content_type: field("content-type"),
        },
    ))
}

async fn write_response(
    mut stream: impl AsyncWrite + Unpin,
    response: Response,
) -> std::io::Result<()> {
    let reason = match response.status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        415 => "Unsupported Media Type",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        _ => "Response",
    };
    // The extension origin is reflected only when the handler validated it
    // via `Response::with_origin`. Anything else gets no ACAO header, so a
    // hostile page cannot read bridge responses even if one reaches the port.
    let cors = response.allow_origin.map(|origin| {
        format!(
            "Access-Control-Allow-Origin: {origin}\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: content-type\r\nAccess-Control-Allow-Private-Network: true\r\n"
        )
    }).unwrap_or_default();
    let header = format!(
        "HTTP/1.1 {} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{cors}Connection: close\r\n\r\n",
        response.status,
        response.body.len()
    );
    stream.write_all(header.as_bytes()).await?;
    stream.write_all(&response.body).await?;
    stream.shutdown().await
}

fn error_response(error: String) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({ "ok": false, "error": error }))
        .unwrap_or_else(|_| b"{\"ok\":false}".to_vec())
}

#[cfg(test)]
mod tests {
    use super::{serve, Request, Response};
    use std::sync::Arc;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
        sync::Notify,
        time::{timeout, Duration},
    };

    #[test]
    fn accepts_next_request_while_handler_is_pending() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        runtime.block_on(async {
            let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
            let address = listener.local_addr().expect("listener address");
            let started = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let handler = {
                let started = Arc::clone(&started);
                let release = Arc::clone(&release);
                move |request: Request| {
                    let started = Arc::clone(&started);
                    let release = Arc::clone(&release);
                    async move {
                        if request.path == "/slow" {
                            started.notify_one();
                            release.notified().await;
                        }
                        Response::new(200, b"{}".to_vec())
                    }
                }
            };
            let server = tokio::spawn(serve(listener, handler));

            let mut slow = TcpStream::connect(address).await.expect("slow connection");
            slow.write_all(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
                .await
                .expect("slow request");
            timeout(Duration::from_secs(1), started.notified())
                .await
                .expect("slow handler started");

            let mut fast = TcpStream::connect(address).await.expect("fast connection");
            fast.write_all(b"GET /fast HTTP/1.1\r\nHost: localhost\r\n\r\n")
                .await
                .expect("fast request");
            let mut fast_response = Vec::new();
            timeout(Duration::from_millis(100), fast.read_to_end(&mut fast_response))
                .await
                .expect("fast request was not blocked by slow handler")
                .expect("fast response");
            assert!(fast_response.starts_with(b"HTTP/1.1 200 OK"));

            release.notify_one();
            let mut slow_response = Vec::new();
            timeout(Duration::from_secs(1), slow.read_to_end(&mut slow_response))
                .await
                .expect("slow response")
                .expect("slow response read");
            assert!(slow_response.starts_with(b"HTTP/1.1 200 OK"));
            server.abort();
            let _ = server.await;
        });
    }

    #[test]
    fn exposes_caller_headers_for_authentication() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        runtime.block_on(async {
            let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
            let address = listener.local_addr().expect("listener address");
            let seen = Arc::new(std::sync::Mutex::new(None));
            let handler = {
                let seen = Arc::clone(&seen);
                move |request: Request| {
                    let seen = Arc::clone(&seen);
                    async move {
                        *seen.lock().expect("seen") = Some((
                            request.host.clone(),
                            request.origin.clone(),
                            request.content_type.clone(),
                        ));
                        Response::new(200, b"{}".to_vec())
                    }
                }
            };
            let server = tokio::spawn(serve(listener, handler));
            let mut client = TcpStream::connect(address).await.expect("client");
            client
                .write_all(
                    b"POST /v1/capture HTTP/1.1\r\nHost: 127.0.0.1:38217\r\nOrigin: chrome-extension://abc\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
                )
                .await
                .expect("request");
            let mut response = Vec::new();
            timeout(Duration::from_secs(1), client.read_to_end(&mut response))
                .await
                .expect("response")
                .expect("response read");
            assert!(response.starts_with(b"HTTP/1.1 200 OK"));
            // No validated origin was reflected, so no ACAO header is present.
            assert!(!response.windows(27).any(|w| w == b"Access-Control-Allow-Origin"));
            let observed = seen.lock().expect("seen").clone().expect("handler ran");
            assert_eq!(observed.0.as_deref(), Some("127.0.0.1:38217"));
            assert_eq!(observed.1.as_deref(), Some("chrome-extension://abc"));
            assert_eq!(observed.2.as_deref(), Some("application/json"));
            server.abort();
            let _ = server.await;
        });
    }
}
