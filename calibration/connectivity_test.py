import unittest
from orchestrator.connectivity import static_connectivity,partition


class ConnectivityTests(unittest.TestCase):
    def obs(self,solids):
        return {'level':{'bounds':[0,0,40,32]},'player':{'hitbox':[0,16,8,11]},'solids':solids,
                'exits':[{'side':'top','y':0,'x':[0,40],'room':'up'},
                         {'side':'left','x':0,'y':[16,32],'room':'back'}]}

    def test_exit_can_have_an_opening_but_be_separated_from_player(self):
        result=static_connectivity(self.obs([[0,12,40,4]]))
        self.assertEqual(result['exits'][0]['relation'],'separated')
        self.assertTrue(result['exits'][0]['openings'])
        self.assertEqual(result['exits'][1]['relation'],'connected')

    def test_projected_neighbor_without_any_opening_is_blocked(self):
        self.assertEqual(static_connectivity(self.obs([[0,0,40,4]]))['exits'][0]['relation'],'blocked')

    def test_low_corridor_is_narrow_not_absolutely_unreachable(self):
        obs={'level':{'bounds':[0,0,40,32]},'player':{'hitbox':[0,16,8,11]},
             'solids':[[16,8,8,24]],'entities':[{'id':1,'type':'Spring','cat':'spring','x':32,'y':16,'w':4,'h':4}]}
        self.assertEqual(static_connectivity(obs)['entities'][0]['relation'],'narrow')

    def test_no_geometry_returns_unknown(self):
        self.assertIsNone(static_connectivity({'level':{'bounds':[0,0,40,32]},'player':{'hitbox':[0,0,8,11]}}))

    def test_graph_cache_does_not_cache_players_region(self):
        obs=self.obs([[0,12,40,4]])
        self.assertEqual(static_connectivity(obs)['exits'][0]['relation'],'separated')
        obs['player']['hitbox']=[0,0,8,11]
        self.assertEqual(static_connectivity(obs)['exits'][0]['relation'],'connected')
