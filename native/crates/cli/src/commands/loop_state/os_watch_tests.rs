//! テスト所有の木を監視する安全な platform 入口。
pub struct Watch(dev_workflow_platform::loop_state_watch::Watch);
impl Watch {
    pub fn new(root: &std::path::Path) -> Self {
        Self(dev_workflow_platform::loop_state_watch::Watch::new(root))
    }
    pub fn finish(self) -> Vec<super::observe::Event> {
        self.0
            .finish()
            .into_iter()
            .map(|(path, mask)| super::observe::Event::OsAccess { path, mask })
            .collect()
    }
}
