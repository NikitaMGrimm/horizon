//! One integrated server tick, ordered with both halves of the local connection.

use super::{EventLoopCtl, Game, Result};
use crate::game::integrated::{ExportLockstep, Integrated};
use horizon_client::net::EXPORT_FENCE;
use horizon_protocol::packets::play::Serverbound;
use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

const TICK_TIMEOUT: Duration = Duration::from_secs(5);

// Delayed echoes from a cancelled export must not complete a new export's turn.
fn next_fence() -> u64 {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    NEXT.fetch_add(1, Ordering::Relaxed)
}

pub(super) struct LiveExport {
    clock: ExportLockstep,
    conn: u64,
    start: Option<(u64, Instant)>,
    steps: u32,
    finish_frame: bool,
    turn: Option<Turn>,
}

struct Turn {
    deadline: Instant,
    fence: u64,
    received: bool,
    tick: Option<u64>,
}

impl LiveExport {
    pub(super) fn new(server: &Integrated, conn: u64) -> Result<Self> {
        let fence = next_fence();
        Ok(Self {
            clock: server.export_lockstep(fence)?,
            conn,
            start: Some((fence, Instant::now() + TICK_TIMEOUT)),
            steps: 0,
            finish_frame: false,
            turn: None,
        })
    }

    pub(super) fn begin_frame(&mut self, steps: u32) {
        self.steps = steps;
        self.finish_frame = true;
    }

    pub(super) fn started(&self) -> bool {
        self.start.is_none()
    }

    fn check(&self, game: &Game<'_>) -> Result<()> {
        self.clock.check()?;
        if game.options.paced_by_split || game.options.split_guest {
            return Err("splitscreen started during export".into());
        }
        if game.replay.playing()
            || !game
                .remote
                .as_ref()
                .is_some_and(|r| r.session.serial() == self.conn)
        {
            return Err("the local connection changed during export".into());
        }
        self.check_deadline(Instant::now())
    }

    fn check_deadline(&self, now: Instant) -> Result<()> {
        if self.start.is_some_and(|(_, deadline)| now >= deadline)
            || self.turn.as_ref().is_some_and(|turn| now >= turn.deadline)
        {
            return Err("the integrated server or its connection stalled during export".into());
        }
        Ok(())
    }
}

impl Game<'_> {
    pub(in crate::game) fn note_export_fence(&mut self, fence: u64) {
        let Some(live) = self.export.as_mut().and_then(|s| s.live.as_mut()) else {
            return;
        };
        if live.start.is_some_and(|(expected, _)| expected == fence) {
            live.start = None;
        } else if let Some(turn) = &mut live.turn
            && turn.fence == fence
        {
            turn.received = true;
        }
    }

    pub(super) fn poll_live_export(&mut self) -> Result<()> {
        if let Some(live) = self.export.as_ref().and_then(|s| s.live.as_ref()) {
            live.check(self)?;
            if self.export_held() {
                self.poll_remote()?;
            }
        }
        Ok(())
    }

    pub(in crate::game) fn take_live_frame_update(&mut self) -> bool {
        self.export
            .as_mut()
            .and_then(|s| s.live.as_mut())
            .is_some_and(|live| std::mem::take(&mut live.finish_frame))
    }

    pub(in crate::game) fn live_export_waiting(&self) -> bool {
        self.export
            .as_ref()
            .and_then(|s| s.live.as_ref())
            .is_some_and(|live| !live.started() || live.turn.is_some())
    }

    pub(super) fn advance_live_export(&mut self, event_loop: &dyn EventLoopCtl) -> Result<bool> {
        loop {
            let Some(live) = self.export.as_ref().and_then(|s| s.live.as_ref()) else {
                // Applying a disconnect can interrupt the session inside poll_remote.
                return Ok(false);
            };
            live.check(self)?;
            if live.steps == 0 {
                return Ok(true);
            }
            let live = self.export.as_mut().and_then(|s| s.live.as_mut()).unwrap();
            if live.turn.is_none() {
                // Everything the client sent before this marker must reach server ingress
                // before we grant the tick that is meant to run it.
                let fence = next_fence();
                self.remote
                    .as_ref()
                    .unwrap()
                    .session
                    .send(Serverbound::Unknown(
                        EXPORT_FENCE,
                        fence.to_be_bytes().to_vec(),
                    ));
                live.turn = Some(Turn {
                    deadline: Instant::now() + TICK_TIMEOUT,
                    fence,
                    received: false,
                    tick: None,
                });
            }
            self.poll_remote()?;
            let Some(live) = self.export.as_mut().and_then(|s| s.live.as_mut()) else {
                return Ok(false);
            };
            if !live.advance_turn()? {
                return Ok(false);
            }
            self.client_tick_steps(1, Some(event_loop))?;
        }
    }
}

impl LiveExport {
    fn advance_turn(&mut self) -> Result<bool> {
        self.clock.check()?;
        let turn = self.turn.as_mut().unwrap();
        if !turn.received {
            return Ok(false);
        }
        let Some(tick) = turn.tick else {
            turn.fence = next_fence();
            turn.received = false;
            turn.tick = Some(self.clock.request(turn.fence)?);
            return Ok(false);
        };
        if !self.clock.completed(tick) {
            return Ok(false);
        }
        self.turn = None;
        self.steps -= 1;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::game::integrated::with_export_clock;

    #[test]
    fn stalled_export_turn_releases_the_server_when_the_session_drains() {
        with_export_clock(|server| {
            let root = tempfile::tempdir().unwrap();
            let mut session = super::super::tests::test_session(root.path());
            let deadline = Instant::now() + TICK_TIMEOUT;
            let mut live = LiveExport::new(server, 1).unwrap();
            live.start = None;
            live.turn = Some(Turn {
                deadline,
                fence: 5,
                received: false,
                tick: Some(live.clock.request(5).unwrap()),
            });
            assert!(
                live.check_deadline(deadline - Duration::from_millis(1))
                    .is_ok()
            );
            assert!(
                live.check_deadline(deadline)
                    .unwrap_err()
                    .to_string()
                    .contains("stalled")
            );
            session.live = Some(live);
            assert!(server.export_lockstep(0).is_err());
            session.begin_drain("failed", None);
            assert!(server.export_lockstep(0).is_ok());
        });
    }
}
