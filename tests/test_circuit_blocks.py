import unittest
from fusion_offline import circuit_blocks as B

BLOCK={'version':1,'id':'front_end','parts':{'R':{'prefix':'R','library_path':'C:/fixture.lbr','evidence_path':'C:/fixture.json','library':'TEST','deviceset':'RES','device':'','units':[{'gate':'G$1','x':0,'y':0,'rotation':0,'sheet':1}]}},
       'nets':{'SIGNAL':[{'part':'R','gate':'G$1','pin':'1'}],'PRIVATE':[{'part':'R','gate':'G$1','pin':'2'}]},
       'ports':{'IN':{'net':'SIGNAL','direction':'in','domain':'analog'}},'parameters':{'spacing':{'min':10,'max':100,'default':20}},'reviews':[]}
class BlocksTest(unittest.TestCase):
    def test_repeat_counts_namespaces_and_determinism(self):
        for count in (1,8,64):
            instances=[{'id':f'CH{i}','x':i*20,'y':0,'parameters':{}} for i in range(count)]
            ports={i['id']:{'IN':'SHARED'} for i in instances}
            ops=B.expand_block(BLOCK,instances,{'R1'},ports)
            self.assertEqual(ops,B.expand_block(BLOCK,instances,{'R1'},ports))
            self.assertEqual(len([o for o in ops if o['op']=='add_part']),count)
            names=[o['net'] for o in ops if o['op']=='connect' and o['net']!='SHARED']
            self.assertEqual(len(set(names)),count)
            self.assertTrue(all(n==n.upper() for n in names))
            self.assertEqual(ops[0]['ref'],'R2')
    def test_ports_ranges_and_collision_are_explicit(self):
        for inst,ports,occupied in [([{'id':'CH','x':0,'y':0,'parameters':{}}],{},set()),
                                    ([{'id':'CH','x':0,'y':0,'parameters':{'spacing':9}}],{'CH':{'IN':'VCC'}},set()),
                                    ([{'id':'CH','x':0,'y':0,'parameters':{}}],{'CH':{'IN':'VCC'}},{'front_end_CH_PRIVATE'})]:
            with self.assertRaises(ValueError): B.expand_block(BLOCK,inst,set(),ports,occupied)
    def test_unreviewed_block_is_blocking(self):
        self.assertTrue(any(f['status']=='unknown' for f in B.validate_block(BLOCK,{},None)))
if __name__=='__main__': unittest.main()
