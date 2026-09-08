use std::future::Future;
use std::sync::Arc;

use tokio::io::{AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

pub const BRIDGE_ADDR: &str = "127.0.0.1:38217";

pub struct Request {
    pub method: String,
    pub path: String,
    pub body: Vec<u8>,
}

pub struct Response {
    status: u16,
    body: Vec<u8>,
}

impl Response {
    pub fn new(status: u16, body: Vec<u8>) -> Self {
        Self { status, body }
    }
}

pub async fn serve<F, Fut>(listener: TcpListener, handler: F) -> std::io::Result<()>
where
    F: Fn(Request) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Response> + Send + 'static,
{
    let handler = Arc::new(handler);
    loop {
        let (stream, _) = listener.accept().await?;
        let handler = Arc::clone(&handler);
        tokio::spawn(async move {
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
    let header_end = loop {
        let mut chunk = [0u8; 8192];
        let read = match stream.read(&mut chunk).await {
            Ok(read) => read,
            Err(error) => return Err((stream, error.to_string())),
        };
        if read == 0 {
            return Err((stream, "request ended before headers were complete".into()));
        }
        bytes.extend_from_slice(&chunk[..read]);
        if let Some(index) = bytes
            .windows(separator.len())
            .position(|window| window == separator)
        {
            break index + separator.len();
        }
        if bytes.len() > HEADER_LIMIT {
            return Err((stream, "request headers are too large".into()));
        }
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
    let content_length = lines
        .filter_map(|line| line.split_once(':'))
        .find_map(|(name, value)| {
            name.eq_ignore_ascii_case("content-length")
                .then_some(value.trim())
        })
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
    while bytes.len() < expected {
        let mut chunk = [0u8; 8192];
        let read = match stream.read(&mut chunk).await {
            Ok(read) => read,
            Err(error) => return Err((stream, error.to_string())),
        };
        if read == 0 {
            return Err((stream, "request ended before the body was complete".into()));
        }
        bytes.extend_from_slice(&chunk[..read]);
    }
    let path = target.split('?').next().unwrap_or(target).to_string();
    Ok((
        stream,
        Request {
            method,
            path,
            body: bytes[header_end..expected].to_vec(),
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
        404 => "Not Found",
        405 => "Method Not Allowed",
        500 => "Internal Server Error",
        _ => "Response",
    };
    let header = format!(
        "HTTP/1.1 {} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: content-type\r\nAccess-Control-Allow-Private-Network: true\r\nConnection: close\r\n\r\n",
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
}
