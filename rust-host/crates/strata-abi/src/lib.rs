//! The Rust side of the Rust/C++ seam. See docs/RUST_HOST.md and include/strata/c/abi.h.
//!
//! This crate is the contract, not the runtime. It holds the version constant the C header mirrors, the status
//! codes the facade returns, and the one data structure the host layer owns outright: the completion ring.
//! Everything here is std-only so it builds without network access.

/// Mirror of `#define STRATA_ABI_VERSION` in include/strata/c/abi.h. tests/abi_header.rs parses the header and
/// fails the build if the two ever disagree — that test is the reason this constant exists twice.
pub const ABI_VERSION: u32 = 1;

/// Mirror of `enum strata_status`. Discriminants are the ABI; the `#[non_exhaustive]` is what lets the C side
/// add a code without silently breaking a match on this one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[non_exhaustive]
pub enum Status {
    Ok = 0,
    WouldBlock = 1,
    BufferTooShort = 2,
    BadSlot = 3,
    DeviceUnavailable = 4,
    AbiVersion = 5,
}

impl Status {
    /// The value the facade hands back. Unknown codes are the other side's bug, not ours — report them, never
    /// guess a variant for them.
    pub fn from_c(raw: i32) -> Option<Status> {
        match raw {
            0 => Some(Status::Ok),
            1 => Some(Status::WouldBlock),
            2 => Some(Status::BufferTooShort),
            3 => Some(Status::BadSlot),
            4 => Some(Status::DeviceUnavailable),
            5 => Some(Status::AbiVersion),
            _ => None,
        }
    }
}

/// A completion queue the host owns. Capacity is fixed at construction because it is the thing the C++ side
/// gets as a plain int today, with a comment about not overflowing it.
///
/// The rule that matters: pushing into a full ring returns `WouldBlock` and does NOT consume or overwrite the
/// entry. A ring that silently drops a completion is the class of bug this whole rewrite exists to kill — the
/// current engine's async tier has produced three of them (see BENCH_NOISE.md for the pattern).
#[derive(Debug)]
pub struct Ring<T> {
    slots: Vec<Option<T>>,
    cap: usize,
    head: usize,
    len: usize,
}

impl<T> Ring<T> {
    pub fn new(cap: usize) -> Ring<T> {
        assert!(cap > 0, "a ring of capacity 0 can never hold anything");
        Ring { slots: (0..cap).map(|_| None).collect(), cap, head: 0, len: 0 }
    }

    pub fn capacity(&self) -> usize { self.cap }
    pub fn len(&self) -> usize { self.len }
    pub fn is_empty(&self) -> bool { self.len == 0 }
    pub fn is_full(&self) -> bool { self.len == self.cap }

    /// Ok(()) on success, Err(Status::WouldBlock) when full — in which case the caller still owns `item`.
    pub fn push(&mut self, item: T) -> Result<(), Status> {
        if self.is_full() {
            return Err(Status::WouldBlock);
        }
        let tail = (self.head + self.len) % self.cap;
        self.slots[tail] = Some(item);
        self.len += 1;
        Ok(())
    }

    pub fn pop(&mut self) -> Option<T> {
        if self.len == 0 {
            return None;
        }
        let slot = self.slots[self.head].take();
        self.head = (self.head + 1) % self.cap;
        self.len -= 1;
        slot
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_ring_refuses_rather_than_dropping() {
        let mut r: Ring<u32> = Ring::new(2);
        assert_eq!(r.push(1), Ok(()));
        assert_eq!(r.push(2), Ok(()));
        assert!(r.is_full());
        // The refusal must be loud, and the item must still belong to the caller.
        assert_eq!(r.push(3), Err(Status::WouldBlock));
        assert_eq!(r.len(), 2);
        assert_eq!(r.pop(), Some(1));
        assert_eq!(r.push(3), Ok(()));
        assert_eq!(r.pop(), Some(2));
        assert_eq!(r.pop(), Some(3));
        assert!(r.is_empty());
    }

    #[test]
    fn wrap_keeps_order() {
        let mut r: Ring<char> = Ring::new(3);
        for _ in 0..5 {
            r.push('a').unwrap();
            r.pop().unwrap();
        }
        r.push('b').unwrap();
        r.push('c').unwrap();
        assert_eq!(r.pop(), Some('b'));
        assert_eq!(r.pop(), Some('c'));
    }

    #[test]
    fn unknown_status_codes_are_not_guessed() {
        assert_eq!(Status::from_c(1), Some(Status::WouldBlock));
        assert_eq!(Status::from_c(6), None);
    }
}
