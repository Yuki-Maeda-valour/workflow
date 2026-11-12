//! Snapshot duplicate traversal and the shared parser-budget type.
use crate::loop_state::graph::{Data, Graph};
pub use crate::loop_state::snapshot_recursion::RecursionPolicy;
use std::collections::HashSet;

/// The Python visitor marks a node before visiting children. In particular, a
/// cyclic alias is skipped and a previously visited branch consumes no descent.
pub(crate) fn check_duplicate_walk(graph: &Graph, policy: RecursionPolicy) -> Result<(), ()> {
    if graph.nodes.is_empty() {
        return Ok(());
    }
    let mut seen = HashSet::new();
    let mut pending = vec![(0, 1usize)];
    while let Some((id, depth)) = pending.pop() {
        if depth.saturating_add(4) > policy.limit {
            return Err(());
        }
        if !seen.insert(id) {
            continue;
        }
        match &graph.nodes[id].data {
            Data::Scalar(_) => {}
            Data::Sequence(items) => {
                pending.extend(items.iter().rev().map(|&child| (child, depth + 1)));
            }
            Data::Mapping(items) => {
                // sum(generator) occurs before the mapping's recursive calls.
                if depth.saturating_add(6) > policy.limit {
                    return Err(());
                }
                for &(key, value) in items.borrow().iter().rev() {
                    pending.push((value, depth + 1));
                    pending.push((key, depth + 1));
                }
            }
        }
    }
    Ok(())
}
