//! Silent diagnostic macro. The compatibility library performs no I/O.
macro_rules! debug_print {
    ($($arg:tt)*) => {{}};
}
