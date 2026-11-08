"""Ender chests and end portals placed by /setblock have block entities (#12752).

EnderChestBlockEntity and TheEndPortalBlockEntity save no fields of their own:
/data reads their id and position, before and after their chunk is saved,
unloaded and loaded. Using the ender chest still opens the player's ender
inventory.
"""

X, Y, Z = 4224, 300, 4096  # far from the player, above any terrain
CHUNK = [[X >> 4, Z >> 4]]
RELOAD_TICKS = 60  # both engines save and drop a chunk once its forced ticket goes


def ender_portal_entities(cmd, setup):
    chest = f'{X + 2} {Y} {Z + 2}'
    portal = f'{X + 6} {Y} {Z + 2}'
    ops = setup(f'/forceload add {X} {Z}')
    ops += [{'op': 'wait_chunks', 'chunks': CHUNK}]
    ops += setup(f'/fill {X} {Y - 1} {Z} {X + 15} {Y - 1} {Z + 15} stone')
    ops += [cmd(f'/execute if block {X + 2} {Y - 1} {Z + 2} stone', expect_result=1),
            cmd(f'/setblock {chest} ender_chest[facing=south]', expect_result=1),
            cmd(f'/setblock {portal} end_portal', expect_result=1)]

    def read():
        return [cmd(f'/data get block {pos}{field}')
                for pos in (chest, portal) for field in ('', ' id')]

    ops += read()
    ops += setup(f'/forceload remove {X} {Z}')
    ops += [{'op': 'tick', 'n': RELOAD_TICKS},
            cmd(f'/execute unless loaded {chest}', expect_result=1)]
    ops += setup(f'/forceload add {X} {Z}')
    ops += [{'op': 'wait_chunks', 'chunks': CHUNK}]
    ops += read()
    # Using the ender chest still opens the ender inventory's 27 slots.
    ops += setup('/gamemode survival', '/clear @s', f'/tp @s {X + 2}.5 {Y} {Z + 4}.5 180 0')
    ops += [{'op': 'tick', 'n': 1},
            cmd(f'/execute if entity @s[x={X + 2}.5,y={Y},z={Z + 4}.5,distance=..0.01]',
                expect_result=1),
            {'op': 'menu_state', 'label': 'before-ender-chest-use',
             'expect_observation': {'own_slots': 0}},
            {'op': 'use_block', 'pos': [X + 2, Y, Z + 2], 'face': 3, 'sequence': 12752},
            {'op': 'menu_state', 'label': 'after-ender-chest-use',
             'expect_observation': {'own_slots': 27}},
            {'op': 'close_menu'}]
    ops += setup('/gamemode creative', '/tp @s 8.5 230 8.5 0 0',
                 f'/fill {X} {Y - 1} {Z} {X + 15} {Y + 1} {Z + 15} air',
                 f'/forceload remove {X} {Z}')
    return ops
