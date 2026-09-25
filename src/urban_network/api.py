"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import parse_qs,urlsplit
from .health import HealthRule
from .models import Reading,Segment
from .service import NetworkService
class Handler(BaseHTTPRequestHandler):
    service=NetworkService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def do_GET(self):
        try:
            url=urlsplit(self.path); path=url.path; qs=parse_qs(url.query); parts=path.split("/")
            if path=="/health":return self._send(200,{"status":"ok","service":"urban-network"})
            if path=="/health-rules":return self._send(200,{"rules":self.service.health_rules(self._token())})
            if path=="/health-reports":return self._send(200,{"reports":self.service.health_reports(self._token())})
            if len(parts)==3 and parts[1]=="health-reports":
                uncertain=qs.get("uncertain",[None])[0]
                return self._send(200,self.service.health_report(self._token(),parts[2],district=qs.get("district",[None])[0],risk_min=qs.get("risk_min",[None])[0],risk_max=qs.get("risk_max",[None])[0],uncertain={"true":True,"false":False}.get(uncertain) if uncertain else None))
            if len(parts)==5 and parts[1]=="health-reports" and parts[3]=="entries":return self._send(200,self.service.health_report_entry(self._token(),parts[2],parts[4]))
            if path.startswith("/segments/") and path.endswith("/risk"):return self._send(200,self.service.risk_report(self._token(),parts[2]))
            if path.startswith("/segments/"):return self._send(200,self.service.segment(self._token(),path.split("/",2)[2]))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
    def do_POST(self):
        try:
            url=urlsplit(self.path); path=url.path; parts=path.split("/")
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token()
            if path=="/segments":return self._send(201,self.service.register_segment(token,Segment(body["segment_id"],body["district"],body["network_type"],body["length_m"],body["criticality"],installed_year=body.get("installed_year"),material=body.get("material",""))))
            if path=="/health-rules":
                rule=HealthRule(rule_version=body["rule_version"],effective_from=body["effective_from"],weight_corrosion=body["weight_corrosion"],weight_repairs=body["weight_repairs"],weight_pressure=body["weight_pressure"],weight_criticality=body["weight_criticality"],corrosion_full_years=body.get("corrosion_full_years",30.0),repair_full_count=body.get("repair_full_count",4),repair_lookback_days=body.get("repair_lookback_days",1095),pressure_full_kpa=body.get("pressure_full_kpa",180.0),min_readings=body.get("min_readings",5),min_observation_days=body.get("min_observation_days",7.0))
                return self._send(201,self.service.create_health_rule(token,rule))
            if len(parts)==4 and parts[1]=="health-rules" and parts[3]=="publish":return self._send(200,self.service.publish_health_rule(token,parts[2]))
            if path=="/health-reports":return self._send(201,self.service.generate_health_report(token,body["name"],body.get("as_of"),body.get("districts")))
            if path.startswith("/segments/") and path.endswith("/readings"):
                sid=parts[2]; r=Reading(body["reading_id"],sid,body["sensor_id"],body["pressure_kpa"],body["flow_lps"],body["acoustic_db"],body["observed_at"]); return self._send(201,self.service.ingest_reading(token,r))
            if path.startswith("/segments/") and path.endswith("/work-orders"):
                return self._send(201,self.service.create_work_order(token,parts[2],body["alert_id"],body["assignee"],body.get("priority",3)))
            return self._send(404,{"error":"not found"})
        except PermissionError as e:return self._send(403,{"error":str(e)})
        except Exception as e:return self._send(400,{"error":str(e)})
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=NetworkService(a.database); Handler.service.bootstrap(); HTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
