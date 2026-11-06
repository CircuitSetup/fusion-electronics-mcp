import unittest
from pathlib import Path
from fusion_offline import project_checks as C, design as D
class ChecksTest(unittest.TestCase):
    def setUp(self): self.sch=D.parse_schematic_design(D.read_xml(str(Path(__file__).parent/'fixtures/mini.sch')))
    def check(self,rule):
        return C.check_project(self.sch,None,{'version':1,'rules':[dict(id='rule',review_status='approved',evidence=['spec#one'],**rule)]})
    def test_counts_and_unresolved(self):
        for n,status in [(2,'pass'),(3,'fail'),(None,'unknown')]: self.assertEqual(self.check({'type':'part_count','selector':{'prefix':'R'},'expected':n})[0]['status'],status)
    def test_connector_reversal(self):
        for net,status in [('VOUT','pass'),('GND','fail')]:
            self.assertEqual(self.check({'type':'connector','ref':'R1','pads':{'2':net}})[0]['status'],status)
    def test_connector_requires_existing_reference_and_pad_expectations(self):
        for ref, pads in [('R1', {}), ('J_DOES_NOT_EXIST', {}), ('J_DOES_NOT_EXIST', {'2': 'VOUT'})]:
            with self.subTest(ref=ref, pads=pads):
                rule = {'type': 'connector', 'ref': ref, 'pads': pads}
                self.assertEqual(self.check(rule)[0]['status'], 'unknown')
    def test_duplicate_channel_aliases(self):
        r=self.check({'type':'channels','assignments':[{'alias':'A','physical':1,'adc':'U1','adc_channel':0,'connector':'J1','position':1},{'alias':'A','physical':2,'adc':'U1','adc_channel':0,'connector':'J1','position':2}]})
        self.assertEqual(r[0]['status'],'fail')
    def test_missing_voltage_rating_is_unknown(self):
        self.assertEqual(self.check({'type':'voltage','net':'VOUT','range':[0,3.3],'ratings':{}})[0]['status'],'unknown')
    def test_isolation_short_detected_from_pin_connectivity(self):
        self.assertEqual(self.check({'type':'isolation','domains':{'analog':[{'part':'R1','gate':'G$1','pin':'P$2'}],'isolated':[{'part':'R2','gate':'G$1','pin':'P$1'}]},'conductive_groups':[],'allowed_crossings':[]})[0]['status'],'fail')
    def test_unknown_physical_acceptance_and_draft(self):
        self.assertEqual(self.check({'type':'physical_clearance'})[0]['status'],'unknown')
        r=C.check_project(self.sch,None,{'version':1,'rules':[{'id':'x','type':'part_count','expected':48,'selector':{},'review_status':'proposed','evidence':['draft']} ]})
        self.assertEqual(r[0]['status'],'unknown')

    def test_unknown_conduction_and_empty_domain_cannot_pass(self):
        from fusion_offline.design import SchNet,PinRef
        self.sch.nets['PRIMARY']=SchNet('PRIMARY','0',pins=[PinRef('R1','G$1','P$1')])
        self.sch.nets['SECONDARY']=SchNet('SECONDARY','0',pins=[PinRef('R2','G$1','P$2')])
        p=lambda r,p:{'part':r,'gate':'G$1','pin':p}
        rule={'type':'isolation','domains':{'primary':[p('R1','P$1')],'secondary':[p('R2','P$2')]},'conductive_groups':[],'allowed_crossings':[]}
        self.assertEqual(self.check(rule)[0]['status'],'unknown')
        rule['conductive_groups']=[[p('R1','P$1'),p('R1','P$2')],[p('R2','P$1'),p('R2','P$2')]]
        self.assertEqual(self.check(rule)[0]['status'],'fail')
        rule['domains']['secondary']=[]
        self.assertEqual(self.check(rule)[0]['status'],'unknown')

if __name__=='__main__': unittest.main()
