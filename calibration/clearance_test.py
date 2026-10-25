"""用完整角色矩形判断直移阻挡，不把绕路连通当成直行畅通。"""
import unittest

from orchestrator.clearance import axis_clearance, clearance_text
from orchestrator.connectivity import static_connectivity


class ClearanceTests(unittest.TestCase):
    def observation(self, solids=(), entities=(), hitbox=(20, 30, 8, 11)):
        return {'player': {'hitbox': list(hitbox)}, 'level': {'bounds': [0, 0, 160, 100]},
                'solids': list(solids), 'entities': list(entities)}

    def test_connected_room_can_have_wall_directly_ahead(self):
        obs = self.observation([[50, 10, 8, 50]])
        self.assertEqual(static_connectivity(obs)['regions'], 1)
        item = axis_clearance(obs)['right']
        self.assertEqual(item['distance'], 22)
        self.assertEqual(item['rect'], [50, 10, 8, 50])

    def test_full_body_overlaps_wall_even_when_center_line_misses_it(self):
        self.assertEqual(axis_clearance(self.observation([[50, 30, 8, 3]]))['right']['distance'], 22)

    def test_floor_contact_does_not_block_horizontal_movement(self):
        result = axis_clearance(self.observation([[0, 41, 160, 8]]))
        self.assertEqual(result['down']['distance'], 0)
        self.assertNotIn('right', result)
        self.assertNotIn('left', result)

    def test_nearest_obstacle_in_each_direction_with_negative_coordinates(self):
        obs = self.observation([[-50, -40, 10, 100], [-25, -40, 5, 100],
                                [10, -40, 8, 100], [-40, -50, 80, 8]], hitbox=(-10, -20, 8, 11))
        result = axis_clearance(obs)
        self.assertEqual(result['left']['distance'], 10)
        self.assertEqual(result['right']['distance'], 12)
        self.assertEqual(result['up']['distance'], 22)

    def test_dream_block_is_conditional_obstacle_not_empty_space(self):
        block = {'id': 1, 'type': 'DreamBlock', 'cat': 'dream_block', 'x': 35, 'y': 20, 'w': 8, 'h': 30, 'active': True}
        result = axis_clearance(self.observation(entities=[block]))['right']
        self.assertEqual(result['distance'], 7)
        self.assertIn('激活状态', result['obstacle'])

    def test_jumpthru_only_blocks_from_above_and_does_not_imply_drop_through(self):
        platform = {'id': 2, 'type': 'JumpthruPlatform', 'cat': 'jumpthru', 'x': 10, 'y': 41, 'w': 50, 'h': 5}
        result = axis_clearance(self.observation(entities=[platform]))
        self.assertEqual(set(result), {'down'})
        self.assertEqual(result['down']['distance'], 0)
        self.assertIn('不表示可按下穿过', result['down']['obstacle'])
        self.assertEqual(axis_clearance(self.observation(entities=[platform], hitbox=(20, 50, 8, 11))), {})

    def test_inactive_colliders_and_collectibles_do_not_block(self):
        entities = [{'id': 1, 'type': 'Block', 'cat': 'solid', 'x': 35, 'y': 20, 'w': 8, 'h': 30, 'collidable': False},
                    {'id': 2, 'type': 'Strawberry', 'cat': 'collectible', 'x': 30, 'y': 20, 'w': 8, 'h': 30}]
        self.assertEqual(axis_clearance(self.observation(entities=entities)), {})

    def test_missing_geometry_does_not_claim_clear_path(self):
        self.assertIsNone(axis_clearance({}))
        self.assertEqual(clearance_text({'player': {'hitbox': [20, 30, 8, 11]}}), [])


if __name__ == '__main__':
    unittest.main()
