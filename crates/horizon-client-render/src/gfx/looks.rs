//! The arenas' quad looks (`mesher::QuadLook`) bound for a shader API 0.2 pack whose terrain
//! reads them (`Report::looks`): one bind group a page, at `GROUP` of its terrain pipelines,
//! set with the page's vertices. A page without looks binds an empty buffer, which the
//! shaders read as no look (`LOOK_WGSL`).

use super::arena::Arena;
use super::flat::TRANSLUCENT_KEYS;

/// The group of a pack's terrain pipelines that holds the looks.
pub const GROUP: u32 = 2;

/// The looks as the material and shadow wrappers read them: `horizon_look(slot)`, a slot's
/// three words (`mesher::QuadLook`), zero past the buffer.
pub const LOOK_WGSL: &str = "
@group(2) @binding(0) var<storage, read> horizon_looks: array<u32>;
fn horizon_look(slot: u32) -> vec3<u32> {
    if slot * 3u + 2u >= arrayLength(&horizon_looks) {
        return vec3<u32>(0u);
    }
    return vec3<u32>(horizon_looks[slot * 3u], horizon_looks[slot * 3u + 1u],
        horizon_looks[slot * 3u + 2u]);
}
";

/// The looks' WGSL for terrain pipelines with the group (`looks`) or without.
pub fn wgsl(looks: bool) -> &'static str {
    if looks { LOOK_WGSL } else { NO_LOOK_WGSL }
}

/// `LOOK_WGSL` for a pack that doesn't read them: every slot has none.
pub const NO_LOOK_WGSL: &str = "
fn horizon_look(slot: u32) -> vec3<u32> {
    return vec3<u32>(0u);
}
";

pub fn layout(device: &wgpu::Device) -> wgpu::BindGroupLayout {
    device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("Terrain quad looks"),
        entries: &[wgpu::BindGroupLayoutEntry {
            binding: 0,
            visibility: wgpu::ShaderStages::VERTEX,
            ty: wgpu::BindingType::Buffer {
                ty: wgpu::BufferBindingType::Storage { read_only: true },
                has_dynamic_offset: false,
                min_binding_size: None,
            },
            count: None,
        }],
    })
}

/// Each page's group, by flat page key (`flat::TRANSLUCENT_KEYS` and up: the translucent
/// arena's pages), made again when the page's buffer changes.
pub struct LookGroups {
    layout: wgpu::BindGroupLayout,
    empty: wgpu::Buffer,
    none: wgpu::BindGroup,
    groups: Vec<Option<(wgpu::Buffer, wgpu::BindGroup)>>,
}

impl LookGroups {
    pub fn new(device: &wgpu::Device, layout: wgpu::BindGroupLayout) -> Self {
        let empty = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("No terrain quad looks"),
            size: 16,
            usage: wgpu::BufferUsages::STORAGE,
            mapped_at_creation: false,
        });
        let none = Self::group(device, &layout, &empty);
        Self {
            layout,
            empty,
            none,
            groups: Vec::new(),
        }
    }

    fn group(
        device: &wgpu::Device,
        layout: &wgpu::BindGroupLayout,
        buffer: &wgpu::Buffer,
    ) -> wgpu::BindGroup {
        device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("Terrain quad looks"),
            layout,
            entries: &[wgpu::BindGroupEntry {
                binding: 0,
                resource: buffer.as_entire_binding(),
            }],
        })
    }

    /// Before each frame's draws: every live page's buffer (a page that grew is another); a
    /// page gone drops its group.
    pub fn refresh(&mut self, device: &wgpu::Device, arena: &Arena, translucent: Option<&Arena>) {
        let pages = arena.page_buffers().map(|(p, _)| (p, arena.looks(p)));
        let split = translucent.into_iter().flat_map(|t| {
            t.page_buffers()
                .map(move |(p, _)| (p + TRANSLUCENT_KEYS, t.looks(p)))
        });
        let mut groups: Vec<Option<(wgpu::Buffer, wgpu::BindGroup)>> = Vec::new();
        for (key, buffer) in pages.chain(split) {
            let key = key as usize;
            if groups.len() <= key {
                groups.resize_with(key + 1, || None);
            }
            let buffer = buffer.unwrap_or(&self.empty);
            groups[key] = match self.groups.get_mut(key).and_then(Option::take) {
                Some((b, group)) if &b == buffer => Some((b, group)),
                _ => Some((buffer.clone(), Self::group(device, &self.layout, buffer))),
            };
        }
        self.groups = groups;
    }

    /// The group of the page with flat page key `key`.
    pub fn get(&self, key: u32) -> &wgpu::BindGroup {
        match self.groups.get(key as usize) {
            Some(Some((_, group))) => group,
            _ => &self.none,
        }
    }

    /// The group of `page` of the translucent arena where there is one (`translucent`), else of
    /// the section arena.
    pub fn page(&self, page: u32, translucent: bool) -> &wgpu::BindGroup {
        self.get(page + if translucent { TRANSLUCENT_KEYS } else { 0 })
    }
}
