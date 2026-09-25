"""协调管网监测、告警、工单和应急资源分配的应用服务。"""
from __future__ import annotations
import hashlib,json,uuid
from datetime import timedelta
from .auth import Auth
from .health import HealthRule,assign_ranks,canonical_json,compute_entry,fingerprint,rule_config_dict
from .models import Reading,Segment,as_dict,parse_time,utcnow
from .risk import leak_probability,score_reading
from .storage import audit,connect,rows,transaction
class NetworkService:
    def __init__(self,database=":memory:"): self.db=connect(database); self.auth=Auth(self.db)
    def bootstrap(self):
        for uid,pwd,role in (("admin","network-admin","admin"),("operator","network-operator","operator")):
            try:self.auth.create_user(uid,pwd,role)
            except Exception:pass
    def register_segment(self,token,segment):
        actor=self.auth.require(token,"admin"); segment.validate(); now=utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO segments(segment_id,district,network_type,length_m,criticality,status,installed_year,material,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(segment.segment_id,segment.district,segment.network_type,segment.length_m,segment.criticality,segment.status,segment.installed_year,segment.material,now,now)); audit(self.db,"segment",segment.segment_id,"created",actor.user_id,as_dict(segment))
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
    def create_health_rule(self,token,rule):
        actor=self.auth.require(token,"approve"); rule.validate()
        config=rule_config_dict(rule); config["effective_from"]=parse_time(rule.effective_from).isoformat()
        with transaction(self.db):
            if self.db.execute("SELECT 1 FROM health_rules WHERE rule_version=?",(rule.rule_version,)).fetchone(): raise ValueError("health rule version already exists")
            self.db.execute("INSERT INTO health_rules VALUES(?,?,?,?,?,?,?,?)",(rule.rule_version,config["effective_from"],canonical_json(config),fingerprint(config),"draft",actor.user_id,utcnow(),None)); audit(self.db,"health_rule",rule.rule_version,"created",actor.user_id,config)
        return self.health_rule(token,rule.rule_version)
    def publish_health_rule(self,token,rule_version):
        actor=self.auth.require(token,"approve")
        with transaction(self.db):
            row=self.db.execute("SELECT status FROM health_rules WHERE rule_version=?",(rule_version,)).fetchone()
            if not row: raise KeyError(rule_version)
            if row[0]!="draft": raise ValueError("only draft rules can be published")
            self.db.execute("UPDATE health_rules SET status='published',published_at=? WHERE rule_version=?",(utcnow(),rule_version)); audit(self.db,"health_rule",rule_version,"published",actor.user_id,{})
        return self.health_rule(token,rule_version)
    def health_rule(self,token,rule_version):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM health_rules WHERE rule_version=?",(rule_version,)).fetchone()
        if not row: raise KeyError(rule_version)
        return self._rule_dict(row)
    def health_rules(self,token):
        self.auth.require(token,"read"); return [self._rule_dict(r) for r in self.db.execute("SELECT * FROM health_rules ORDER BY effective_from,rule_version").fetchall()]
    def generate_health_report(self,token,name,as_of=None,districts=None):
        actor=self.auth.require(token,"analyze")
        if not name or not name.strip(): raise ValueError("report name is required")
        as_of=parse_time(as_of).isoformat() if as_of else utcnow(); as_of_dt=parse_time(as_of)
        rule_row=self.db.execute("SELECT * FROM health_rules WHERE status='published' AND effective_from<=? ORDER BY effective_from DESC,rowid DESC LIMIT 1",(as_of,)).fetchone()
        if not rule_row: raise ValueError("no published health rule is effective at the report time")
        rule=HealthRule(**json.loads(rule_row["config"]))
        if districts:
            if not all(isinstance(d,str) and d.strip() for d in districts): raise ValueError("district filters are invalid")
            marks=",".join("?" for _ in districts); segments=rows(self.db,f"SELECT * FROM segments WHERE district IN ({marks}) ORDER BY segment_id",sorted(set(districts)))
        else: segments=rows(self.db,"SELECT * FROM segments ORDER BY segment_id")
        entries=[]; segment_inputs=[]
        for seg in segments:
            readings=[r for r in rows(self.db,"SELECT * FROM readings WHERE segment_id=?",(seg["segment_id"],)) if parse_time(r["observed_at"])<=as_of_dt]
            readings.sort(key=lambda r:(parse_time(r["observed_at"]),r["reading_id"]))
            window_start=as_of_dt-timedelta(days=rule.repair_lookback_days)
            orders=[o for o in rows(self.db,"SELECT * FROM work_orders WHERE segment_id=? AND status='completed'",(seg["segment_id"],)) if window_start<=parse_time(o["updated_at"])<=as_of_dt]
            orders.sort(key=lambda o:o["work_order_id"])
            entries.append(compute_entry(seg,readings,orders,rule,as_of))
            segment_inputs.append({"segment":{k:seg[k] for k in ("segment_id","district","network_type","length_m","criticality","status","installed_year","material")},"readings":[[r["reading_id"],r["observed_at"],r["pressure_kpa"],r["flow_lps"],r["acoustic_db"]] for r in readings],"work_orders":[[o["work_order_id"],o["status"],o["updated_at"]] for o in orders]})
        assign_ranks(entries)
        report_id="hr-"+uuid.uuid4().hex[:16]
        input_fp=fingerprint({"rule":json.loads(rule_row["config"]),"as_of":as_of,"segments":segment_inputs})
        params={"districts":sorted(set(districts)) if districts else None}
        summary={"segments":len(entries),"confident":sum(1 for e in entries if not e["uncertain"]),"uncertain":sum(1 for e in entries if e["uncertain"])}
        with transaction(self.db):
            self.db.execute("INSERT INTO health_reports VALUES(?,?,?,?,?,?,?,?,?,?)",(report_id,name.strip(),as_of,rule.rule_version,rule_row["fingerprint"],input_fp,canonical_json(params),canonical_json(summary),actor.user_id,utcnow()))
            for e in entries:
                self.db.execute("INSERT INTO health_report_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(report_id,e["segment_id"],e["district"],e["health_index"],e["risk_score"],e["risk_band"],canonical_json(e["contributions"]),canonical_json(e["factors"]),1 if e["uncertain"] else 0,json.dumps(e["uncertainty_reasons"],ensure_ascii=False),e["rank"],json.dumps(e["reading_ids"],ensure_ascii=False),json.dumps(e["work_order_ids"],ensure_ascii=False)))
            audit(self.db,"health_report",report_id,"generated",actor.user_id,{"name":name.strip(),"as_of":as_of,"rule_version":rule.rule_version,"input_fingerprint":input_fp})
        return self.health_report(token,report_id)
    def health_reports(self,token):
        self.auth.require(token,"read"); return [self._report_dict(r) for r in self.db.execute("SELECT * FROM health_reports ORDER BY generated_at DESC,report_id").fetchall()]
    def health_report(self,token,report_id,district=None,risk_min=None,risk_max=None,uncertain=None):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM health_reports WHERE report_id=?",(report_id,)).fetchone()
        if not row: raise KeyError(report_id)
        query="SELECT * FROM health_report_entries WHERE report_id=?"; args=[report_id]
        if district: query+=" AND district=?"; args.append(district)
        if risk_min is not None: query+=" AND risk_score>=?"; args.append(float(risk_min))
        if risk_max is not None: query+=" AND risk_score<=?"; args.append(float(risk_max))
        if uncertain is not None: query+=" AND uncertain=?"; args.append(1 if uncertain else 0)
        query+=" ORDER BY rank IS NULL,rank,segment_id"
        return {"report":self._report_dict(row),"entries":[self._entry_dict(r) for r in self.db.execute(query,args).fetchall()]}
    def health_report_entry(self,token,report_id,segment_id):
        self.auth.require(token,"read"); row=self.db.execute("SELECT * FROM health_report_entries WHERE report_id=? AND segment_id=?",(report_id,segment_id)).fetchone()
        if not row: raise KeyError(f"{report_id}/{segment_id}")
        entry=self._entry_dict(row)
        return {"entry":entry,"readings":self._fetch_by_ids("readings","reading_id",entry["reading_ids"]),"work_orders":self._fetch_by_ids("work_orders","work_order_id",entry["work_order_ids"])}
    def _fetch_by_ids(self,table,column,ids):
        if not ids: return []
        marks=",".join("?" for _ in ids); found={r[column]:dict(r) for r in self.db.execute(f"SELECT * FROM {table} WHERE {column} IN ({marks})",ids).fetchall()}
        return [found[i] for i in ids if i in found]
    def _rule_dict(self,row):
        data=dict(row); data["config"]=json.loads(data["config"]); return data
    def _report_dict(self,row):
        data=dict(row); data["params"]=json.loads(data["params"]); data["summary"]=json.loads(data["summary"]); return data
    def _entry_dict(self,row):
        data=dict(row); data["uncertain"]=bool(data["uncertain"])
        for key in ("contributions","factors","uncertainty_reasons","reading_ids","work_order_ids"): data[key]=json.loads(data[key])
        return data
