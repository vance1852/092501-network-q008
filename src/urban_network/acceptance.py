"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json
from .health import HealthRule
from .models import Reading,Segment
from .service import NetworkService
def run():
    s=NetworkService(); s.bootstrap(); t=s.auth.login("admin","network-admin"); s.register_segment(t,Segment("SEG-DEMO","north","water",680,5,installed_year=2004,material="cast-iron")); r=s.ingest_reading(t,Reading("RD-DEMO","SEG-DEMO","sensor-01",160,230,88,"2026-09-24T10:00:00+00:00")); report=s.risk_report(t,"SEG-DEMO"); order=s.create_work_order(t,"SEG-DEMO",r["alert_id"],"crew-north",1); s.add_resource(t,"PUMP-01","mobile-pump","north",2); allocation=s.allocate(t,"PUMP-01",order["work_order_id"],1)
    s.create_health_rule(t,HealthRule("v2026.1","2026-01-01T00:00:00+00:00",0.3,0.25,0.25,0.2,min_readings=1,min_observation_days=0)); s.publish_health_rule(t,"v2026.1"); health=s.generate_health_report(t,"annual-baseline"); entry=health["entries"][0]
    return {"status":"ok","segment":"SEG-DEMO","severity":r["risk"]["severity"],"probability":report["leak_probability"],"allocation":allocation["allocation_id"],"health_report":health["report"]["report_id"],"health_index":entry["health_index"],"risk_band":entry["risk_band"]}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
