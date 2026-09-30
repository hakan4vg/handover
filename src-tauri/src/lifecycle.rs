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
