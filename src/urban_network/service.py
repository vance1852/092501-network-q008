"""协调管网监测、告警、工单和应急资源分配的应用服务。"""
from __future__ import annotations
import hashlib,json,uuid
from .auth import Auth
from .health import DEFAULT_CONFIG, canonical_json, evaluate, input_fingerprint, rule_fingerprint, validate_config
from .models import Reading,Segment,as_dict,parse_time,utcnow
from .risk import leak_probability,score_reading
from .storage import audit,connect,rows,transaction
class NetworkService:
    def __init__(self,database=":memory:"): self.db=connect(database); self.auth=Auth(self.db)
    def bootstrap(self):
        for uid,pwd,role in (("admin","network-admin","admin"),("operator","network-operator","operator")):
            try:self.auth.create_user(uid,pwd,role)
            except Exception:pass
        # 内置基线健康评分规则，保证离线验收可复现；可再发布新版本或新规则。
        if not self.db.execute("SELECT 1 FROM health_rules LIMIT 1").fetchone():
            self.db.execute("INSERT INTO health_rules VALUES(?,?,?,?,?,?,?,?)",("baseline-health",1,"2026-01-01T00:00:00+00:00",json.dumps(DEFAULT_CONFIG,ensure_ascii=False,sort_keys=True),rule_fingerprint("baseline-health",1,"2026-01-01T00:00:00+00:00",DEFAULT_CONFIG),1,"system",utcnow())); self.db.commit()
    def register_segment(self,token,segment):
        actor=self.auth.require(token,"admin"); segment.validate(); now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",(segment.segment_id,segment.district,segment.network_type,segment.length_m,segment.criticality,segment.status,segment.install_year,segment.material,now,now)); audit(self.db,"segment",segment.segment_id,"created",actor.user_id,as_dict(segment))
        return self.segment(token,segment.segment_id)
    def segment(self,token,segment_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM segments WHERE segment_id=?",(segment_id,)).fetchone()
        if not row:raise KeyError(segment_id)
        return dict(row)
    def ingest_reading(self,token,reading):
        actor=self.auth.require(token,"measure"); reading.validate(); seg=self.db.execute("SELECT criticality FROM segments WHERE segment_id=?",(reading.segment_id,)).fetchone()
        if not seg:raise KeyError(reading.segment_id)
        risk=score_reading(reading.pressure_kpa,reading.flow_lps,reading.acoustic_db,seg[0]); fingerprint=hashlib.sha256(f"{reading.segment_id}|{reading.sensor_id}|{reading.observed_at}".encode()).hexdigest()
        with transaction(self.db):
            if self.db.execute("SELECT reading_id FROM readings WHERE reading_id=?",(reading.reading_id,)).fetchone(): return {"reading_id":reading.reading_id,"duplicate":True,"risk":as_dict(risk)}
            self.db.execute("INSERT INTO readings VALUES(?,?,?,?,?,?,?)",(reading.reading_id,reading.segment_id,reading.sensor_id,reading.pressure_kpa,reading.flow_lps,reading.acoustic_db,reading.observed_at)); alert_id=None
            if risk.severity in {"high","critical"}:
                alert_id="alert-"+fingerprint[:18]; self.db.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?,?,?)",(alert_id,reading.segment_id,fingerprint,risk.severity,risk.score,"open",utcnow(),None))
            audit(self.db,"reading",reading.reading_id,"ingested",actor.user_id,{"risk":as_dict(risk),"alert_id":alert_id})
        return {"reading_id":reading.reading_id,"duplicate":False,"risk":as_dict(risk),"alert_id":alert_id}
    def risk_report(self,token,segment_id):
        self.auth.require(token,"analyze"); readings=rows(self.db,"SELECT * FROM readings WHERE segment_id=? ORDER BY observed_at",(segment_id,)); alerts=rows(self.db,"SELECT * FROM alerts WHERE segment_id=? ORDER BY created_at",(segment_id,)); return {"segment_id":segment_id,"readings":len(readings),"alerts":alerts,"leak_probability":leak_probability(alerts)}
    # ---- 健康评分规则（可配置、带生效时间、版本不可变） ----
    def publish_health_rule(self,token,rule_id,config,effective_at):
        actor=self.auth.require(token,"approve")
        if not rule_id.strip(): raise ValueError("rule id is required")
        validate_config(config); effective=parse_time(effective_at).isoformat()
        with transaction(self.db):
            if self.db.execute("SELECT 1 FROM health_rules WHERE rule_id=? AND effective_at=?",(rule_id,effective)).fetchone(): raise ValueError("a rule version with this effective_at already exists")
            row=self.db.execute("SELECT COALESCE(MAX(version),0) FROM health_rules WHERE rule_id=?",(rule_id,)).fetchone(); version=row[0]+1
            fp=rule_fingerprint(rule_id,version,effective,config); now=utcnow()
            self.db.execute("INSERT INTO health_rules VALUES(?,?,?,?,?,?,?,?)",(rule_id,version,effective,json.dumps(config,ensure_ascii=False,sort_keys=True),fp,1,actor.user_id,now)); audit(self.db,"health_rule",rule_id,"published",actor.user_id,{"version":version,"effective_at":effective,"fingerprint":fp})
        return {"rule_id":rule_id,"version":version,"effective_at":effective,"config":config,"fingerprint":fp,"active":True}
    def list_health_rules(self,token):
        self.auth.require(token,"read"); result={}
        for row in rows(self.db,"SELECT * FROM health_rules WHERE active=1 ORDER BY rule_id,version"):
            row["config"]=json.loads(row["config"]); result.setdefault(row["rule_id"],[]).append(row)
        return [{"rule_id":rid,"versions":versions} for rid,versions in result.items()]
    def _resolve_rule(self,rule_id,as_of_iso):
        if rule_id is None:
            row=self.db.execute("SELECT rule_id FROM health_rules WHERE active=1 AND effective_at<=? ORDER BY effective_at DESC,version DESC LIMIT 1",(as_of_iso,)).fetchone()
            if not row: raise LookupError("no scoring rule is effective yet")
            rule_id=row[0]
        row=self.db.execute("SELECT * FROM health_rules WHERE rule_id=? AND active=1 AND effective_at<=? ORDER BY version DESC LIMIT 1",(rule_id,as_of_iso)).fetchone()
        if not row: raise LookupError(f"no effective version of rule {rule_id}")
        return dict(rule_id=rule_id,version=row["version"],effective_at=row["effective_at"],config=json.loads(row["config"]),fingerprint=row["fingerprint"])
    def health_rule(self,token,rule_id,version=None):
        self.auth.require(token,"read")
        if version is not None:
            row=self.db.execute("SELECT * FROM health_rules WHERE rule_id=? AND version=?",(rule_id,version)).fetchone()
            if not row: raise KeyError(f"{rule_id} v{version}")
            row=dict(row); row["config"]=json.loads(row["config"]); return row
        return self._resolve_rule(rule_id,utcnow())
    # ---- 健康报告：冻结规则版本、输入指纹与各项贡献 ----
    def generate_health_report(self,token,district=None,rule_id=None,as_of=None):
        actor=self.auth.require(token,"analyze")
        as_of_dt=parse_time(as_of) if as_of else parse_time(utcnow()); as_of_iso=as_of_dt.isoformat()
        rule=self._resolve_rule(rule_id,as_of_iso)
        segment_sql="SELECT * FROM segments"+(" WHERE district=?" if district else ""); args=(district,) if district else ()
        segments=rows(self.db,segment_sql+" ORDER BY segment_id",args)
        snapshot={"as_of":as_of_iso,"rule_id":rule["rule_id"],"rule_version":rule["version"],"rule_fingerprint":rule["fingerprint"],"segments":{}}
        entries=[]
        for segment in segments:
            sid=segment["segment_id"]
            readings=rows(self.db,"SELECT * FROM readings WHERE segment_id=? AND observed_at<=? ORDER BY observed_at,reading_id",(sid,as_of_iso))
            work_orders=rows(self.db,"SELECT * FROM work_orders WHERE segment_id=? AND created_at<=? ORDER BY created_at,work_order_id",(sid,as_of_iso))
            result=evaluate(segment,readings,work_orders,rule["config"],as_of_dt)
            snapshot["segments"][sid]={"segment":segment,"readings":readings,"work_orders":work_orders}
            entries.append((sid,segment["district"],result,readings,work_orders))
        fp=input_fingerprint(snapshot)
        # 确定性样本充足的管段按分数排名；样本不足的不赋予名次，单独分组。
        ranked=sorted((e for e in entries if not e[2]["uncertain"]),key=lambda e:(-e[2]["score"],e[0]))
        uncertain=sorted((e for e in entries if e[2]["uncertain"]),key=lambda e:(-e[2]["score"],e[0]))
        ordered=ranked+uncertain
        report_id="hr-"+uuid.uuid4().hex[:16]; now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO health_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(report_id,rule["rule_id"],rule["version"],rule["fingerprint"],rule["effective_at"],json.dumps(rule["config"],ensure_ascii=False,sort_keys=True),fp,json.dumps(snapshot,ensure_ascii=False,sort_keys=True),district,as_of_iso,len(entries),len(uncertain),actor.user_id,now))
            for position,(sid,district_value,result,readings,work_orders) in enumerate(ordered,1):
                self.db.execute("INSERT INTO health_report_segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(report_id,sid,result["score"],result["risk_band"],1 if result["uncertain"] else 0,result["confidence"],json.dumps(list(result["reasons"]),ensure_ascii=False),json.dumps(result["contributions"],ensure_ascii=False,sort_keys=True),result["samples"]["readings"],result["samples"]["repairs"],json.dumps([r["reading_id"] for r in readings]),json.dumps([w["work_order_id"] for w in work_orders]),position if not result["uncertain"] else None))
            audit(self.db,"health_report",report_id,"generated",actor.user_id,{"rule_id":rule["rule_id"],"rule_version":rule["version"],"input_fingerprint":fp,"segments":len(entries),"uncertain":len(uncertain)})
        return self.health_report(token,report_id)
    def list_health_reports(self,token):
        self.auth.require(token,"read")
        return rows(self.db,"SELECT report_id,rule_id,rule_version,input_fingerprint,district,as_of,segment_count,uncertain_count,generated_at FROM health_reports ORDER BY generated_at DESC")
    def health_report(self,token,report_id,district=None,risk_band=None,min_score=None,max_score=None):
        self.auth.require(token,"read")
        header=self.db.execute("SELECT * FROM health_reports WHERE report_id=?",(report_id,)).fetchone()
        if not header: raise KeyError(report_id)
        query="SELECT * FROM health_report_segments WHERE report_id=?"; args=[report_id]
        if district: query+=" AND segment_id IN (SELECT segment_id FROM segments WHERE district=?)"; args.append(district)
        if risk_band: query+=" AND risk_band=?"; args.append(risk_band)
        if min_score is not None: query+=" AND score>=?"; args.append(float(min_score))
        if max_score is not None: query+=" AND score<=?"; args.append(float(max_score))
        query+=" ORDER BY rank_position IS NOT NULL DESC, COALESCE(rank_position,1000000000), score DESC, segment_id"
        segment_rows=[]
        for row in rows(self.db,query,tuple(args)):
            for key in ("reasons","contributions","reading_ids","work_order_ids"): row[key]=json.loads(row[key])
            row["uncertain"]=bool(row["uncertain"]); segment_rows.append(row)
        return {"report_id":report_id,"rule_id":header["rule_id"],"rule_version":header["rule_version"],"rule_fingerprint":header["rule_fingerprint"],"rule_effective_at":header["effective_at"],"config":json.loads(header["config"]),"input_fingerprint":header["input_fingerprint"],"as_of":header["as_of"],"district_scope":header["district"],"segment_count":header["segment_count"],"uncertain_count":header["uncertain_count"],"generated_at":header["generated_at"],"returned_count":len(segment_rows),"segments":segment_rows}
    def health_report_trace(self,token,report_id,segment_id):
        """沿冻结报告追溯该管段当时使用的读数与工单（取自报告输入快照）。"""
        self.auth.require(token,"read")
        header=self.db.execute("SELECT input_snapshot,input_fingerprint FROM health_reports WHERE report_id=?",(report_id,)).fetchone()
        if not header: raise KeyError(report_id)
        snapshot=json.loads(header["input_snapshot"])
        if segment_id not in snapshot["segments"]: raise KeyError(segment_id)
        frozen=snapshot["segments"][segment_id]
        verified=input_fingerprint(snapshot)==header["input_fingerprint"]
        return {"report_id":report_id,"segment_id":segment_id,"fingerprint_verified":verified,"segment":frozen["segment"],"readings":frozen["readings"],"work_orders":frozen["work_orders"]}
    def create_work_order(self,token,segment_id,alert_id,assignee,priority=3):
        actor=self.auth.require(token,"work_order")
        if not assignee.strip() or not 1<=priority<=5:raise ValueError("assignee and priority are invalid")
        if not self.db.execute("SELECT 1 FROM alerts WHERE alert_id=? AND segment_id=?",(alert_id,segment_id)).fetchone():raise KeyError(alert_id)
        wid="wo-"+uuid.uuid4().hex[:16]
        with transaction(self.db): self.db.execute("INSERT INTO work_orders VALUES(?,?,?,?,?,?,?,?)",(wid,segment_id,alert_id,assignee,"open",priority,utcnow(),utcnow())); audit(self.db,"work_order",wid,"created",actor.user_id,{"segment_id":segment_id,"alert_id":alert_id})
        return self.work_order(token,wid)
    def work_order(self,token,work_order_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone()
        if not row:raise KeyError(work_order_id)
        return dict(row)
    def transition_work_order(self,token,work_order_id,target,reason):
        actor=self.auth.require(token,"work_order"); allowed={"open":{"assigned","cancelled"},"assigned":{"in_progress","cancelled"},"in_progress":{"completed","blocked"},"blocked":{"in_progress","cancelled"},"completed":set(),"cancelled":set()}
        if not reason.strip():raise ValueError("transition reason is required")
        with transaction(self.db):
            row=self.db.execute("SELECT status FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone()
            if not row:raise KeyError(work_order_id)
            if target not in allowed.get(row[0],set()):raise ValueError("invalid work order transition")
            self.db.execute("UPDATE work_orders SET status=?,updated_at=? WHERE work_order_id=?",(target,utcnow(),work_order_id)); audit(self.db,"work_order",work_order_id,"transition",actor.user_id,{"from":row[0],"to":target,"reason":reason})
        return self.work_order(token,work_order_id)
    def add_resource(self,token,resource_id,kind,district,capacity):
        actor=self.auth.require(token,"admin")
        if capacity<=0 or not kind.strip() or not district.strip():raise ValueError("resource fields are invalid")
        with transaction(self.db):self.db.execute("INSERT INTO resources VALUES(?,?,?,?,?)",(resource_id,kind,district,capacity,capacity)); audit(self.db,"resource",resource_id,"created",actor.user_id,{"kind":kind,"district":district,"capacity":capacity})
        return self.resource(token,resource_id)
    def resource(self,token,resource_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM resources WHERE resource_id=?",(resource_id,)).fetchone()
        if not row:raise KeyError(resource_id)
        return dict(row)
    def allocate(self,token,resource_id,work_order_id,quantity):
        actor=self.auth.require(token,"allocate")
        if quantity<=0:raise ValueError("quantity must be positive")
        aid="alloc-"+uuid.uuid4().hex[:16]
        with transaction(self.db):
            resource=self.db.execute("SELECT available FROM resources WHERE resource_id=?",(resource_id,)).fetchone()
            if not resource:raise KeyError(resource_id)
            if not self.db.execute("SELECT 1 FROM work_orders WHERE work_order_id=?",(work_order_id,)).fetchone():raise KeyError(work_order_id)
            if resource[0]<quantity:raise ValueError("resource capacity exceeded")
            old=self.db.execute("SELECT allocation_id FROM allocations WHERE resource_id=? AND work_order_id=?",(resource_id,work_order_id)).fetchone()
            if old:return {"allocation_id":old[0],"duplicate":True}
            self.db.execute("INSERT INTO allocations VALUES(?,?,?,?,?)",(aid,resource_id,work_order_id,quantity,utcnow())); self.db.execute("UPDATE resources SET available=available-? WHERE resource_id=?",(quantity,resource_id)); audit(self.db,"resource",resource_id,"allocated",actor.user_id,{"work_order_id":work_order_id,"quantity":quantity})
        return {"allocation_id":aid,"duplicate":False,"resource_id":resource_id,"quantity":quantity}
    def audit_events(self,token,entity_type,entity_id): self.auth.require(token,"read"); return rows(self.db,"SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",(entity_type,entity_id))
