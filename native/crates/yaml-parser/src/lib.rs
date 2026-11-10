// Copyright 2015, Yuheng Chen.
// Copyright 2023, Ethiraric.
// See the LICENSE file at the top-level directory of this distribution.

//! Fixed event parser for loop-state's legacy YAML grammar.
//!
//! Derived from saphyr-parser 0.1.2; see UPSTREAM.md for provenance and changes.
//! Scalars retain Python Unicode code points, including escaped surrogates.

#![warn(missing_docs, clippy::pedantic)]
#![no_std]
#![forbid(unsafe_code)]

#[macro_use]
extern crate alloc;

mod char_traits;
#[macro_use]
mod debug;
pub mod input;
mod parser;
mod scanner;

pub use crate::input::{BufferedInput, Input, str::StrInput};
pub use crate::parser::{Event, EventReceiver, Parser, SpannedEventReceiver, Tag};
pub use crate::scanner::{Marker, ScalarStyle, ScanError, Span};
