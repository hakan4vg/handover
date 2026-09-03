use futures_util::future::{AbortHandle, AbortRegistration};
use std::collections::HashMap;
use std::sync::Mutex;

pub(crate) fn state_allows_transfer(state: Option<&str>) -> bool {
    matches!(
        state,
        Some("connecting") | Some("downloading") | Some("finalizing")
    )
}

#[derive(Default)]
pub(crate) struct TransferRegistry {
    controls: Mutex<HashMap<String, AbortHandle>>,
}

impl TransferRegistry {
    pub(crate) fn claim(&self, id: &str) -> Option<AbortRegistration> {
        let (abort, registration) = AbortHandle::new_pair();
        let mut controls = self.controls.lock().ok()?;
        if controls.contains_key(id) {
            return None;
        }
        controls.insert(id.to_string(), abort);
        Some(registration)
    }

    pub(crate) fn release(&self, id: &str) {
        if let Ok(mut controls) = self.controls.lock() {
            controls.remove(id);
        }
    }

    pub(crate) fn abort(&self, id: &str) {
        if let Ok(controls) = self.controls.lock() {
            if let Some(control) = controls.get(id) {
                control.abort();
            }
        }
    }

    pub(crate) fn is_active(&self, id: &str) -> bool {
        self.controls
            .lock()
            .map(|controls| controls.contains_key(id))
            .unwrap_or(true)
    }
}

#[cfg(test)]
mod tests {
    use super::{state_allows_transfer, TransferRegistry};
    use std::future::Future;
    use std::task::{Context, Poll, Waker};

    #[test]
    fn transfer_claim_is_exclusive_until_released() {
        let registry = TransferRegistry::default();

        assert!(registry.claim("job-1").is_some());
        assert!(registry.claim("job-1").is_none());

        registry.release("job-1");
        assert!(registry.claim("job-1").is_some());
    }

    #[test]
    fn concurrent_claims_leave_one_transfer_owner() {
        let registry = std::sync::Arc::new(TransferRegistry::default());
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let winners = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut threads = Vec::new();

        for _ in 0..8 {
            let registry = std::sync::Arc::clone(&registry);
            let barrier = std::sync::Arc::clone(&barrier);
            let winners = std::sync::Arc::clone(&winners);
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                if registry.claim("job-1").is_some() {
                    winners.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
            }));
        }
        for thread in threads {
            thread.join().expect("claim thread");
        }

        assert_eq!(winners.load(std::sync::atomic::Ordering::Relaxed), 1);
        assert!(registry.is_active("job-1"));
    }

    #[test]
    fn aborting_claim_cancels_pending_transfer_and_allows_reclaim() {
        let registry = TransferRegistry::default();
        let registration = registry.claim("job-1").expect("first owner");
        let mut transfer = Box::pin(futures_util::future::Abortable::new(
            std::future::pending::<()>(),
            registration,
        ));
        let waker = Waker::noop();
        let mut context = Context::from_waker(waker);

        assert!(matches!(
            transfer.as_mut().poll(&mut context),
            Poll::Pending
        ));
        registry.abort("job-1");
        assert!(matches!(
            transfer.as_mut().poll(&mut context),
            Poll::Ready(Err(_))
        ));

        registry.release("job-1");
        assert!(registry.claim("job-1").is_some());
    }

    #[test]
    fn only_active_persisted_states_allow_transfer_work() {
        for state in ["connecting", "downloading", "finalizing"] {
            assert!(
                state_allows_transfer(Some(state)),
                "{state} should continue"
            );
        }
        for state in [
            Some("paused"),
            Some("cancelled"),
            Some("completed"),
            Some("failed"),
            None,
        ] {
            assert!(!state_allows_transfer(state), "{state:?} should stop");
        }
    }
}
