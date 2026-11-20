//! Safe graphics after a start that ended in graphics device loss (#19206). The window's device
//! loss writes [`MARKER`] beside `horizon-graphics.txt` (`Game::recover_device`); a recovery that
//! keeps presenting frames for [`HEALTHY_AFTER_RECOVERY`], or a clean exit, removes it. The next
//! start turns a marker it finds into a hold kept in that file ([`SafeStart`], key `safeStart`),
//! one step further each time the device is lost again: HDR output held back ([`Step::Hdr`]),
//! then the other graphics API as well ([`Step::OtherApi`]). The saved settings stay as the
//! player chose them. Choosing an HDR mode, another graphics API or a new driver drops the hold.
//! Offscreen renderers and split-screen guests have no window driver: they never write the marker.
use crate::graphics::{FILE, Graphics, GraphicsApi};
use std::time::{Duration, Instant};

/// Written beside [`FILE`] when the window's graphics device is lost (a [`Loss`]), and before a
/// start on the other graphics API ([`TRYING`]) until it presents a frame.
pub const MARKER: &str = "horizon-device-lost";
/// The marker of a start on the other graphics API that hasn't presented a frame yet: a native
/// crash in that API's driver leaves no loss and no crash report, and would otherwise repeat at
/// every start.
pub const TRYING: &str = "trying-other-api\n";
/// How long a recovered device keeps presenting frames before its loss is forgiven: the same
/// minute in which `Game::recover_device` gives up on a second loss.
pub const HEALTHY_AFTER_RECOVERY: Duration = Duration::from_secs(60);

/// The window's graphics device was lost.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Loss {
    /// The window's graphics driver (`output::window_driver`).
    pub driver: String,
    /// The graphics API setting the start ran with.
    pub setting: GraphicsApi,
    /// The API the device was opened on (`Auto` for one with no other, Metal).
    pub backend: GraphicsApi,
    /// HDR output was on.
    pub hdr: bool,
}

/// What a start finds in [`MARKER`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Marker {
    Loss(Loss),
    /// A start on the other graphics API that ended before it presented a frame.
    Trying,
}

/// How far a start steps back.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    /// HDR output held back on the driver.
    Hdr,
    /// HDR output held back, and the start opens its device on the other API.
    OtherApi,
    /// HDR output held back on the API the device was lost on: the other API didn't present a
    /// frame, or there is none. Nothing further to try.
    OtherApiFailed,
}
impl Step {
    fn name(self) -> &'static str {
        match self {
            Step::Hdr => "hdr",
            Step::OtherApi => "api",
            Step::OtherApiFailed => "api-failed",
        }
    }
}

/// The hold a device loss left (`Graphics::safe_start`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SafeStart {
    pub step: Step,
    /// The graphics API setting it was made under: another one drops it.
    pub setting: GraphicsApi,
    /// The API the device was lost on.
    pub lost: GraphicsApi,
    /// The window driver it holds on; empty until the next window names one (a start on the
    /// other API reads another driver). Another driver drops it.
    pub driver: String,
}

/// The API a start uses instead of `lost` on `os` (`std::env::consts::OS`), as the renderer's
/// own search orders them (`device::backend_order`); `None` where there is no other.
pub fn other_api(lost: GraphicsApi, os: &str) -> Option<GraphicsApi> {
    use GraphicsApi::*;
    match (os, lost) {
        ("macos", _) | (_, Auto) => None,
        ("windows", Dx12) => Some(Vulkan),
        // OpenGL as the launcher moves it after a driver crash (#18291).
        ("windows", Vulkan | OpenGl) => Some(Dx12),
        (_, Vulkan) => Some(OpenGl),
        (_, OpenGl) => Some(Vulkan),
        (_, Dx12) => None,
    }
}

/// The marker's text for a loss on a window whose driver is `driver`; `None` offscreen, which
/// never writes one.
pub fn loss_text(
    driver: Option<&str>,
    setting: GraphicsApi,
    backend: GraphicsApi,
    hdr: bool,
) -> Option<String> {
    Some(format!(
        "driver:{}\nsetting:{}\nbackend:{}\nhdr:{hdr}\n",
        driver?,
        setting.name(),
        backend.name()
    ))
}

/// What a marker's text says; `None` for one it can't read.
pub fn parse_marker(text: &str) -> Option<Marker> {
    if text == TRYING {
        return Some(Marker::Trying);
    }
    let field = |key: &str| {
        text.lines()
            .find_map(|line| line.strip_prefix(key)?.strip_prefix(':'))
    };
    Some(Marker::Loss(Loss {
        driver: field("driver")?.trim().to_owned(),
        setting: GraphicsApi::from_name(field("setting")?)?,
        backend: GraphicsApi::from_name(field("backend")?)?,
        hdr: field("hdr")?.trim() == "true",
    }))
}

/// The hold as [`FILE`] keeps it: `<step>,<setting>,<lost>,<driver>`.
pub fn record_text(s: &SafeStart) -> String {
    format!(
        "{},{},{},{}",
        s.step.name(),
        s.setting.name(),
        s.lost.name(),
        s.driver
    )
}

pub fn parse_record(text: &str) -> Option<SafeStart> {
    let mut parts = text.splitn(4, ',');
    let step = match parts.next()? {
        "hdr" => Step::Hdr,
        "api" => Step::OtherApi,
        "api-failed" => Step::OtherApiFailed,
        _ => return None,
    };
    Some(SafeStart {
        step,
        setting: GraphicsApi::from_name(parts.next()?)?,
        lost: GraphicsApi::from_name(parts.next()?)?,
        driver: parts.next()?.trim().to_owned(),
    })
}

/// The hold the start after `marker` runs with, given the one in force (`held`), on `os`, and
/// the line saying what changed (`None`: nothing).
pub fn after(
    held: Option<&SafeStart>,
    marker: &Marker,
    os: &str,
) -> (Option<SafeStart>, Option<String>) {
    let loss = match marker {
        Marker::Trying => {
            return match held {
                Some(s) if s.step == Step::OtherApi => {
                    let tried = other_api(s.lost, os).map_or("another graphics API", |a| a.label());
                    (
                        Some(SafeStart {
                            step: Step::OtherApiFailed,
                            driver: String::new(),
                            ..s.clone()
                        }),
                        Some(format!(
                            "the last start on {tried} ended before it showed a frame: this start goes back to {} with HDR output still held back",
                            s.lost.label()
                        )),
                    )
                }
                _ => (held.cloned(), None),
            };
        }
        Marker::Loss(loss) => loss,
    };
    // A hold made under another API setting is the player's earlier choice, not this one's.
    let held = held.filter(|s| s.setting == loss.setting);
    let hold = |step, driver: &str| SafeStart {
        step,
        setting: loss.setting,
        lost: loss.backend,
        driver: driver.to_owned(),
    };
    let api = loss.backend.label();
    if loss.hdr {
        return (
            Some(hold(Step::Hdr, &loss.driver)),
            Some(format!(
                "the last start lost the graphics device on {api} with HDR output on: HDR output is held back on that driver"
            )),
        );
    }
    match (held.map(|s| s.step), other_api(loss.backend, os)) {
        (Some(Step::OtherApi | Step::OtherApiFailed), _) => (
            held.cloned(),
            Some(format!(
                "the last start lost the graphics device on {api} again, with HDR output held back and the other graphics API tried: nothing safer is left to try"
            )),
        ),
        (_, Some(other)) => (
            Some(hold(Step::OtherApi, "")),
            Some(format!(
                "the last start lost the graphics device on {api} with HDR output off or held back: this start uses {}",
                other.label()
            )),
        ),
        (_, None) => (
            Some(hold(Step::OtherApiFailed, &loss.driver)),
            Some(format!(
                "the last start lost the graphics device on {api} with HDR output off or held back, and there is no other graphics API to try here"
            )),
        ),
    }
}

impl Graphics {
    /// Takes the [`MARKER`] in `dir` into the hold, saved with the settings there, and leaves
    /// [`TRYING`] for a start on the other graphics API.
    pub(crate) fn take_device_loss(&mut self, dir: &std::path::Path) {
        let marker = dir.join(MARKER);
        if let Ok(text) = std::fs::read_to_string(&marker)
            && std::fs::remove_file(&marker).is_ok()
            && let Some(found) = parse_marker(&text)
        {
            let (held, line) = after(self.safe_start.as_ref(), &found, std::env::consts::OS);
            if let Some(line) = line {
                log::warn!("Horizon renderer: {line}");
            }
            if held != self.safe_start {
                self.safe_start = held;
                self.save(dir);
            }
        }
        if self
            .safe_start
            .as_ref()
            .is_some_and(|s| s.step == Step::OtherApi)
        {
            let _ = std::fs::write(&marker, TRYING);
        }
    }

    /// The device-loss hold in force: one made under another graphics API setting is not.
    pub fn safe_start(&self) -> Option<&SafeStart> {
        self.safe_start.as_ref().filter(|s| s.setting == self.api)
    }

    /// Whether the device-loss hold keeps HDR output back on a window whose driver is `driver`.
    pub(crate) fn safe_start_holds_hdr(&self, driver: &str) -> bool {
        self.safe_start()
            .is_some_and(|s| s.driver.is_empty() || s.driver == driver)
    }

    /// The window's renderer runs on `driver`: the hold that names no driver yet is this one's,
    /// and a hold on another driver (an update, another GPU) or made under another graphics API
    /// setting is dropped, so the start is tried as chosen again. True when it changed.
    pub(crate) fn settle_safe_start(&mut self, driver: &str) -> bool {
        let Some(s) = &mut self.safe_start else {
            return false;
        };
        if s.setting != self.api || !(s.driver.is_empty() || s.driver == driver) {
            self.safe_start = None;
        } else if s.driver.is_empty() {
            s.driver = driver.to_owned();
        } else {
            return false;
        }
        true
    }

    /// The graphics API a window's start opens its device on for the setting `api`: the other
    /// one while the device-loss hold says so, said once.
    pub(crate) fn start_api(&self, api: GraphicsApi) -> GraphicsApi {
        let Some(s) = self.safe_start().filter(|s| s.step == Step::OtherApi) else {
            return api;
        };
        let Some(other) = other_api(s.lost, std::env::consts::OS) else {
            return api;
        };
        static LOGGED: std::sync::Once = std::sync::Once::new();
        LOGGED.call_once(|| {
            log::warn!(
                "Horizon renderer: starting on {} instead of {}: the graphics device was lost on {} with HDR output off or held back; the graphics API setting stays {} (choose one, or remove safeStart from {FILE}, to try it again)",
                other.label(),
                s.lost.label(),
                s.lost.label(),
                self.api.label()
            )
        });
        other
    }
}

/// Removes this run's [`MARKER`] beside `options` (the player's `options.txt`).
pub fn clear(options: &std::path::Path) {
    let _ = std::fs::remove_file(options.with_file_name(MARKER));
}

/// When the run's [`MARKER`] may go as frames are presented: at the first one for a start's own
/// ([`TRYING`]), [`HEALTHY_AFTER_RECOVERY`] after a recovery for a loss.
#[derive(Clone, Copy, Debug)]
pub struct Proof(Option<Instant>);
impl Proof {
    pub fn started(now: Instant) -> Self {
        Self(Some(now))
    }
    pub fn recovered(&mut self, now: Instant) {
        self.0 = Some(now + HEALTHY_AFTER_RECOVERY);
    }
    /// A frame was presented at `now`: true, once, when the marker can go.
    pub fn presented(&mut self, now: Instant) -> bool {
        let due = self.0.is_some_and(|at| now >= at);
        if due {
            self.0 = None;
        }
        due
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use GraphicsApi::*;

    const NV: &str = "Dx12 10de:25a9 NVIDIA GeForce RTX 2050; ; 32.0.16.2124; Windows 11";
    const NV_VK: &str = "Vulkan 10de:25a9 NVIDIA GeForce RTX 2050; NVIDIA; 581.57; Windows 11";

    fn loss(hdr: bool, backend: GraphicsApi) -> Marker {
        let text = loss_text(Some(NV), Dx12, backend, hdr).unwrap();
        parse_marker(&text).unwrap()
    }

    /// Offscreen writes nothing; what a window writes reads back, and so does the hold.
    #[test]
    fn offscreen_writes_no_marker_and_records_round_trip() {
        assert_eq!(loss_text(None, Dx12, Dx12, true), None);
        assert_eq!(
            loss(true, Dx12),
            Marker::Loss(Loss {
                driver: NV.into(),
                setting: Dx12,
                backend: Dx12,
                hdr: true
            })
        );
        assert_eq!(parse_marker(TRYING), Some(Marker::Trying));
        assert_eq!(parse_marker("driver:x\n"), None);
        let held = SafeStart {
            step: Step::OtherApi,
            setting: Auto,
            lost: Dx12,
            driver: "a, b; c".into(),
        };
        assert_eq!(parse_record(&record_text(&held)), Some(held));
    }

    /// No hold, a loss with HDR on: HDR held on that driver. Lost again with it held: the other
    /// API. Lost on that one too: it stays, with nothing further. The other API that never
    /// showed a frame: back to the first API, HDR still held, and a loss there escalates no more.
    #[test]
    fn each_loss_steps_further_back() {
        let os = "windows";
        let (first, line) = after(None, &loss(true, Dx12), os);
        let first = first.unwrap();
        assert_eq!((first.step, first.driver.as_str()), (Step::Hdr, NV));
        assert!(line.is_some());

        let (second, _) = after(Some(&first), &loss(false, Dx12), os);
        let second = second.unwrap();
        assert_eq!((second.step, second.lost), (Step::OtherApi, Dx12));
        assert_eq!(second.driver, "");
        let mut g = Graphics {
            api: Dx12,
            safe_start: Some(second.clone()),
            ..Graphics::default()
        };
        assert_eq!(g.start_api(Dx12), Vulkan);

        let (third, _) = after(Some(&second), &loss(false, Vulkan), os);
        assert_eq!(third.as_ref(), Some(&second));

        let (failed, line) = after(Some(&second), &Marker::Trying, os);
        let failed = failed.unwrap();
        assert_eq!((failed.step, failed.lost), (Step::OtherApiFailed, Dx12));
        assert!(line.is_some());
        g.safe_start = Some(failed.clone());
        assert_eq!(g.start_api(Dx12), Dx12);
        assert!(g.safe_start_holds_hdr(NV));
        assert_eq!(after(Some(&failed), &loss(false, Dx12), os).0, Some(failed));
        // A start that finds its own trying marker with no hold to step back from changes nothing.
        assert_eq!(after(None, &Marker::Trying, os), (None, None));
    }

    /// HDR already off: straight to the other API, where there is one.
    #[test]
    fn a_loss_with_hdr_off_goes_to_the_other_api_where_there_is_one() {
        let other = |os| after(None, &loss(false, Vulkan), os).0.unwrap().step;
        assert_eq!(other("windows"), Step::OtherApi);
        assert_eq!(other("linux"), Step::OtherApi);
        assert_eq!(other("macos"), Step::OtherApiFailed);
        assert_eq!(other_api(Vulkan, "linux"), Some(OpenGl));
        assert_eq!(other_api(Auto, "windows"), None);
    }

    /// The hold applies on its driver only, never offscreen (no driver), and goes with another
    /// driver or another graphics API setting; a choice of HDR mode drops it on the HDR page.
    #[test]
    fn the_hold_goes_with_the_driver_or_the_api_setting() {
        let held = after(None, &loss(true, Dx12), "windows").0;
        let g = Graphics {
            api: Dx12,
            hdr: crate::graphics::HdrMode::On,
            safe_start: held,
            ..Graphics::default()
        };
        assert!(super::super::output::held(&g, Some(NV)));
        assert!(!super::super::output::held(&g, None));
        assert!(!g.safe_start_holds_hdr(NV_VK));

        let mut same = g.clone();
        assert!(!same.settle_safe_start(NV));
        assert!(same.safe_start.is_some());

        let mut updated = g.clone();
        assert!(updated.settle_safe_start(NV_VK));
        assert_eq!(updated.safe_start, None);

        let mut changed = Graphics { api: Vulkan, ..g };
        assert!(changed.safe_start().is_none());
        assert!(changed.settle_safe_start(NV));
        assert_eq!(changed.safe_start, None);

        // The other API's start names its driver at its first window, and keeps it.
        let mut other = Graphics {
            api: Dx12,
            safe_start: after(None, &loss(false, Dx12), "windows").0,
            ..Graphics::default()
        };
        assert!(other.safe_start_holds_hdr(NV_VK));
        assert!(other.settle_safe_start(NV_VK));
        assert_eq!(other.safe_start.as_ref().unwrap().driver, NV_VK);
    }

    /// A loss is forgiven only after a minute of frames following the recovery; a start's own
    /// marker goes at its first frame.
    #[test]
    fn the_marker_goes_after_a_healthy_minute_or_the_first_frame() {
        let t = Instant::now();
        let mut proof = Proof::started(t);
        assert!(proof.presented(t));
        assert!(!proof.presented(t + Duration::from_secs(1)));
        proof.recovered(t);
        assert!(!proof.presented(t + Duration::from_secs(59)));
        assert!(proof.presented(t + HEALTHY_AFTER_RECOVERY));
        assert!(!proof.presented(t + Duration::from_secs(120)));
    }
}
