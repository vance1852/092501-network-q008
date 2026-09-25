"""依赖标准库的 JSON HTTP API。"""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
from .models import Reading,Segment
from .service import NetworkService
class Handler(BaseHTTPRequestHandler):
    service=NetworkService()
    def _send(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _token(self):return self.headers.get("Authorization","").removeprefix("Bearer ")
    def _query(self,key,default=None): return self._params.get(key,[default])[0]
    def _error(self,error):
        if isinstance(error,PermissionError): return self._send(403,{"error":str(error)})
        if isinstance(error,(KeyError,LookupError)): return self._send(404,{"error":str(error)})
        return self._send(400,{"error":str(error)})
    def do_GET(self):
        try:
            parsed=urlparse(self.path); self.path=parsed.path; self._params=parse_qs(parsed.query)
            if self.path=="/health":return self._send(200,{"status":"ok","service":"urban-network"})
            if self.path=="/health-rules":return self._send(200,self.service.list_health_rules(self._token()))
            if self.path.startswith("/health-rules/"):
                version=self._query("version"); rule_id=self.path[len("/health-rules/"):]; return self._send(200,self.service.health_rule(self._token(),rule_id,int(version) if version else None))
            if self.path=="/health-reports":return self._send(200,self.service.list_health_reports(self._token()))
            if self.path.startswith("/health-reports/"):
                rest=self.path[len("/health-reports/"):].split("/")
                if len(rest)==4 and rest[1]=="segments" and rest[3]=="trace":
                    return self._send(200,self.service.health_report_trace(self._token(),rest[0],rest[2]))
                if len(rest)==1:
                    return self._send(200,self.service.health_report(self._token(),rest[0],district=self._query("district"),risk_band=self._query("risk_band"),min_score=self._query("min_score"),max_score=self._query("max_score")))
                return self._send(404,{"error":"not found"})
            if self.path.startswith("/segments/") and self.path.endswith("/risk"):return self._send(200,self.service.risk_report(self._token(),self.path.split("/")[2]))
            if self.path.startswith("/segments/"):return self._send(200,self.service.segment(self._token(),self.path.split("/",2)[2]))
            return self._send(404,{"error":"not found"})
        except Exception as e:return self._error(e)
    def do_POST(self):
        try:
            body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))) or b"{}")
            if self.path=="/login":return self._send(200,{"token":self.service.auth.login(body["user_id"],body["password"])})
            token=self._token()
            if self.path=="/segments":return self._send(201,self.service.register_segment(token,Segment(body["segment_id"],body["district"],body["network_type"],body["length_m"],body["criticality"],body.get("status","normal"),body.get("install_year"),body.get("material","steel"))))
            if self.path=="/health-rules":return self._send(201,self.service.publish_health_rule(token,body["rule_id"],body["config"],body["effective_at"]))
            if self.path=="/health-reports":return self._send(201,self.service.generate_health_report(token,district=body.get("district"),rule_id=body.get("rule_id"),as_of=body.get("as_of")))
            if self.path.startswith("/segments/") and self.path.endswith("/readings"):
                sid=self.path.split("/")[2]; r=Reading(body["reading_id"],sid,body["sensor_id"],body["pressure_kpa"],body["flow_lps"],body["acoustic_db"],body["observed_at"]); return self._send(201,self.service.ingest_reading(token,r))
            if self.path.startswith("/segments/") and self.path.endswith("/work-orders"):
                return self._send(201,self.service.create_work_order(token,self.path.split("/")[2],body["alert_id"],body["assignee"],body.get("priority",3)))
            return self._send(404,{"error":"not found"})
        except Exception as e:return self._error(e)
def main():
    p=argparse.ArgumentParser(); p.add_argument("--database",default=":memory:"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args(); Handler.service=NetworkService(a.database); Handler.service.bootstrap(); ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
if __name__=="__main__":main()
