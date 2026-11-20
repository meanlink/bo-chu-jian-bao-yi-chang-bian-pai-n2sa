import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit
from src.models.diary import DiaryBuild
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import diary as diary_service
from src.schemas.diary import BatchInput, BuildInput, WiringInput, WiringEdgeInput
from src.utils.security import hash_password
from src.utils.serialization import checksum

# 链路与上游都带日记：主路A/主路B 是新闻一频的两路上游
OUTAGE_ROWS = [
    # 11-16：主路A 停播，新闻一频与文化二频依次截断 → 正常中继截断
    ('2026-11-16', '主路A', 24.0), ('2026-11-16', '主路B', 0.0),
    ('2026-11-16', '新闻一频', 23.0), ('2026-11-16', '文化二频', 21.0), ('2026-11-16', '城市直播', 0.0),
    # 11-17：全链路正常，用于验证无停播日不生成组
    ('2026-11-17', '主路A', 0.0), ('2026-11-17', '主路B', 0.0),
    ('2026-11-17', '新闻一频', 0.0), ('2026-11-17', '文化二频', 0.0), ('2026-11-17', '城市直播', 0.0),
    # 11-18：主路A 缺报，新闻一频与文化二频停播 → 待核
    ('2026-11-18', '主路B', 0.0), ('2026-11-18', '新闻一频', 19.0),
    ('2026-11-18', '文化二频', 18.0), ('2026-11-18', '城市直播', 0.0),
    # 11-19：主路A 与主路B 同时停播，新闻一频停得更多 → 多个疑似源头全列出
    ('2026-11-19', '主路A', 12.0), ('2026-11-19', '主路B', 15.0),
    ('2026-11-19', '新闻一频', 30.0), ('2026-11-19', '文化二频', 27.0), ('2026-11-19', '城市直播', 0.0),
    # 11-20：城市直播自身停播，改用 11-20 起生效的接线
    ('2026-11-20', '主路A', 0.0), ('2026-11-20', '主路B', 0.0),
    ('2026-11-20', '新闻一频', 0.0), ('2026-11-20', '文化二频', 0.0), ('2026-11-20', '城市直播', 31.0),
]

SUPPLEMENT_ROWS = [
    # 补报 11-18 全日日记（补齐主路A）：只影响这一天，其余日沿用生效版本
    ('2026-11-18', '主路A', 20.0), ('2026-11-18', '主路B', 0.0),
    ('2026-11-18', '新闻一频', 19.0), ('2026-11-18', '文化二频', 18.0),
    ('2026-11-18', '城市直播', 0.0),
]


def _record_rows(pairs):
    rows = []
    for day, channel, downtime in pairs:
        planned = 720.0 if channel != '城市直播' else 360.0
        rows.append({
            'day': day, 'channel': channel, 'planned_min': planned,
            'aired_min': round(planned - downtime, 2), 'interruption_min': downtime, 'reach': 168000,
        })
    return rows


def outage_records():
    return _record_rows(OUTAGE_ROWS)


def _seed_outage(db, admin):
    if db.scalar(select(DiaryBuild).limit(1)):
        return
    rows = outage_records()
    dataset = db.scalar(select(Dataset).where(Dataset.name == '海棠融媒_停播归并_1120.csv'))
    if not dataset:
        dataset = Dataset(name='海棠融媒_停播归并_1120.csv', source='outage-20261120.csv',
                          records=rows, checksum=checksum(rows), created_at=time.time())
        db.add(dataset); db.flush()
    supplement_dataset = db.scalar(select(Dataset).where(Dataset.name == '海棠融媒_停播归并_补报1118.csv'))
    if not supplement_dataset:
        supplement_rows = _record_rows(SUPPLEMENT_ROWS)
        supplement_dataset = Dataset(name='海棠融媒_停播归并_补报1118.csv', source='outage-20261118-supp.csv',
                                     records=supplement_rows, checksum=checksum(supplement_rows),
                                     created_at=time.time())
        db.add(supplement_dataset); db.flush()

    actor = admin.email
    # 11-16 起：新闻一频由主路A/主路B双路上游供给，文化二频中继自新闻一频
    diary_service.register_wiring(db, WiringInput(
        effective_from='2026-11-16',
        edges=[WiringEdgeInput(upstream='主路A', downstream='新闻一频'),
               WiringEdgeInput(upstream='主路B', downstream='新闻一频'),
               WiringEdgeInput(upstream='新闻一频', downstream='文化二频'),
               WiringEdgeInput(upstream='备路转播', downstream='城市直播')],
        sources=['备路转播'], note='主备双路供新闻一频'), actor)
    # 11-20 起：城市直播再并入卫星备份，接线变化只影响 11-20 及以后
    diary_service.register_wiring(db, WiringInput(
        effective_from='2026-11-20',
        edges=[WiringEdgeInput(upstream='主路A', downstream='新闻一频'),
               WiringEdgeInput(upstream='主路B', downstream='新闻一频'),
               WiringEdgeInput(upstream='新闻一频', downstream='文化二频'),
               WiringEdgeInput(upstream='备路转播', downstream='城市直播'),
               WiringEdgeInput(upstream='卫星上行', downstream='城市直播')],
        sources=['备路转播', '卫星上行'], note='城市直播并入卫星备份'), actor)

    base = diary_service.create_batch(db, BatchInput(
        dataset_id=dataset.id, kind='base', label='11-16~11-20 基础批'), actor)
    build, _ = diary_service.open_build(db, BuildInput(batch_id=base.id, threshold=10), actor)
    diary_service.run_build(db, build)
    diary_service.publish(db, build.id)

    supplement = diary_service.create_batch(db, BatchInput(
        dataset_id=supplement_dataset.id, kind='supplement', parent_batch_id=base.id,
        label='补报 11-18 全日日记'), actor)
    supp_build, _ = diary_service.open_build(
        db, BuildInput(batch_id=supplement.id, threshold=10), actor)
    # 算齐但不自动切换：演示“一次性切换”由负责人执行，期间仍读基础版本。
    diary_service.run_build(db, supp_build)
    db.add(Audit(actor='seed', action='demo.diary',
                 detail={'base': build.id, 'supplement': supp_build.id}, at=time.time()))


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        admin=db.scalar(select(User).where(User.role=='admin'))
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_outage(db, admin)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,DiaryBuild]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
