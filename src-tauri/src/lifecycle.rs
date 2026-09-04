use futures_util::future::{AbortHandle, AbortRegistration};
use std::collections::HashMap;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Mutex,
};

pub(crate) fn state_allows_transfer(state: Option<&str>) -> bool {
    matches!(
        state,
        Some("connecting") | Some("downloading") | Some("finalizing")
    )
}

struct TransferOwner {
    generation: u64,
    abort: AbortHandle,
}

#[derive(Default)]
pub(crate) struct TransferRegistry {
    controls: Mutex<HashMap<String, TransferOwner>>,
    next_generation: AtomicU64,
}

impl TransferRegistry {
    pub(crate) fn claim_with_generation(&self, id: &str) -> Option<(u64, AbortRegistration)> {
        let mut controls = self.controls.lock().ok()?;
        if controls.contains_key(id) {
            return None;
        }
        let generation = self.next_generation.fetch_add(1, Ordering::Relaxed);
        let (abort, registration) = AbortHandle::new_pair();
        controls.insert(id.to_string(), TransferOwner { generation, abort });
        Some((generation, registration))
    }

    pub(crate) fn release_if_current(&self, id: &str, generation: u64) {
        if let Ok(mut controls) = self.controls.lock() {
            if controls
                .get(id)
                .is_some_and(|owner| owner.generation == generation)
            {
                controls.remove(id);
            }
        }
    }

    pub(crate) fn abort(&self, id: &str) {
        if let Ok(mut controls) = self.controls.lock() {
            if let Some(owner) = controls.remove(id) {
                owner.abort.abort();
            }
        }
    }

    pub(crate) fn is_current(&self, id: &str, generation: u64) -> bool {
        self.controls
            .lock()
            .map(|controls| {
                controls
                    .get(id)
                    .is_some_and(|owner| owner.generation == generation)
            })
            .unwrap_or(false)
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

        let (generation, _) = registry
            .claim_with_generation("job-1")
            .expect("first owner");
        assert!(registry.claim_with_generation("job-1").is_none());

        registry.release_if_current("job-1", generation);
        assert!(registry.claim_with_generation("job-1").is_some());
    }

    #[test]
    fn stale_owner_cannot_release_replacement() {
        let registry = TransferRegistry::default();
        let (first_generation, _) = registry
            .claim_with_generation("job-1")
            .expect("first owner");
        registry.release_if_current("job-1", first_generation);
        let (second_generation, _) = registry
            .claim_with_generation("job-1")
            .expect("replacement owner");

        assert_ne!(first_generation, second_generation);
        assert!(!registry.is_current("job-1", first_generation));
        registry.release_if_current("job-1", first_generation);
        assert!(registry.is_current("job-1", second_generation));
        registry.release_if_current("job-1", second_generation);
        assert!(!registry.is_current("job-1", second_generation));
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
                if registry.claim_with_generation("job-1").is_some() {
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
        let registration = registry
            .claim_with_generation("job-1")
            .expect("first owner")
            .1;
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

        assert!(registry.claim_with_generation("job-1").is_some());
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
