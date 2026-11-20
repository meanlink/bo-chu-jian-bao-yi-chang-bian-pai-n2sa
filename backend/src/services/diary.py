"""停播日记归并的持久化与编排。

关键约定：
- 接线关系按生效日登记为版本快照；每个停播日使用“生效日不晚于当天”的最新一版；
- 归并版本创建时锁定接线水位（wiring_watermark），重试与中断恢复都沿用同一批日记和当时的接线；
- 同一批次 + 同一接线水位 + 同一阈值只对应一个版本，重试复用而不新增版本号；
- 补报只重算受补报影响的组，其余组从已生效版本原样复制并标注复用来源；
- 全部组算齐后一次性切换为生效版本；切换前查询仍读旧版，旧版永久保留可照查；
- 每组都保存当日接线快照与所用日记行，历版可逐日核对。
"""
import time
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select, func, update

from src.models import Dataset
from src.models.diary import DiaryBatch, DiaryBuild, OutageGroup, WiringVersion
from src.services import outage_logic
from src.utils.serialization import checksum

DEFAULT_THRESHOLD = 10.0

STATUS_LABEL = {
    'draft': '待计算',
    'building': '计算中',
    'ready': '待切换',
    'published': '生效中',
    'failed': '计算失败',
}


# --------------------------------------------------------------------------
# 接线关系
# --------------------------------------------------------------------------

def wiring_at(db, day, watermark=None):
    """当日生效的接线版本：生效日不晚于当天，且不高于构建时锁定的水位。"""
    query = select(WiringVersion).where(WiringVersion.effective_from <= str(day))
    if watermark:
        query = query.where(WiringVersion.id <= watermark)
    return db.scalar(query.order_by(
        WiringVersion.effective_from.desc(), WiringVersion.id.desc()).limit(1))


def wiring_watermark(db):
    return db.scalar(select(func.max(WiringVersion.id))) or 0


def wiring_payload(row):
    if row is None:
        return None
    return {
        'id': row.id,
        'effective_from': row.effective_from,
        'edges': row.edges,
        'sources': row.sources,
        'note': row.note,
        'created_by': row.created_by,
        'created_at': row.created_at,
    }


def register_wiring(db, data, actor):
    try:
        day = date.fromisoformat(str(data.effective_from)).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, '生效日须为 ISO 日期，例如 2026-11-20') from None
    if day > date.today().isoformat() and not data.allow_future:
        raise HTTPException(422, '生效日不能晚于今天；若确为预告接线，请勾选“预告生效”')
    edges, sources = outage_logic.normalize_wiring({
        'edges': [{'upstream': e.upstream, 'downstream': e.downstream} for e in (data.edges or [])],
        'sources': data.sources or [],
    })
    error = outage_logic.validate_wiring(edges, sources)
    if error:
        raise HTTPException(422, error)
    row = WiringVersion(
        effective_from=day,
        edges=[{'upstream': upstream, 'downstream': downstream} for upstream, downstream in edges],
        sources=sources,
        note=(data.note or '').strip()[:500],
        created_by=actor,
        created_at=time.time(),
    )
    db.add(row)
    db.flush()
    return row


def wiring_history(db):
    rows = db.scalars(select(WiringVersion).order_by(
        WiringVersion.effective_from.asc(), WiringVersion.id.asc()))
    return [wiring_payload(row) for row in rows]


# --------------------------------------------------------------------------
# 日记批次
# --------------------------------------------------------------------------

def _dataset(db, dataset_id):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')
    return dataset


def dataset_index(dataset):
    """{播出日: {链路: [行, ...]}}"""
    index = {}
    for row in dataset.records or []:
        day = str(row.get('day'))
        channel = str(row.get('channel'))
        index.setdefault(day, {}).setdefault(channel, []).append(row)
    return index


def create_batch(db, data, actor):
    dataset = _dataset(db, data.dataset_id)
    parent = None
    if data.kind == 'supplement':
        if not data.parent_batch_id:
            raise HTTPException(422, '补报批次须指定所补的基础批次')
        parent = db.get(DiaryBatch, data.parent_batch_id)
        if not parent:
            raise HTTPException(404, '所补的基础批次不存在')
        if parent.kind != 'base':
            raise HTTPException(422, '补报只能挂在基础批次下')
    elif data.parent_batch_id:
        raise HTTPException(422, '基础批次不需要指定上级批次')
    row = DiaryBatch(
        dataset_id=dataset.id,
        kind=data.kind,
        parent_batch_id=parent.id if parent else None,
        label=(data.label or '').strip()[:200],
        checksum=checksum(dataset.records),
        created_by=actor,
        created_at=time.time(),
    )
    db.add(row)
    db.flush()
    return row


def batch_payload(db, row):
    dataset = db.get(Dataset, row.dataset_id)
    index = dataset_index(dataset) if dataset else {}
    days = sorted(index)
    channels = sorted({channel for day in index.values() for channel in day})
    return {
        'id': row.id,
        'dataset_id': row.dataset_id,
        'dataset_name': dataset.name if dataset else '',
        'kind': row.kind,
        'parent_batch_id': row.parent_batch_id,
        'label': row.label,
        'checksum': row.checksum,
        'created_by': row.created_by,
        'created_at': row.created_at,
        'days': len(days),
        'first_day': days[0] if days else None,
        'last_day': days[-1] if days else None,
        'channels': channels,
    }


# --------------------------------------------------------------------------
# 日记合并视图
# --------------------------------------------------------------------------

def chain_of(db, batch):
    """返回 (基础批次, 截至该批次的补报批次列表)。"""
    if batch.kind == 'supplement' and batch.parent_batch_id:
        base = db.get(DiaryBatch, batch.parent_batch_id)
        supplements = list(db.scalars(select(DiaryBatch).where(
            DiaryBatch.parent_batch_id == base.id,
            DiaryBatch.id <= batch.id,
        ).order_by(DiaryBatch.id.asc())))
        return base, supplements
    return batch, []


def merged_index(db, batch):
    """基础批次 + 补报批次的最新生效视图；补报按“链路 + 日”整行覆盖。"""
    base, supplements = chain_of(db, batch)
    merged = {}
    for source in [base] + supplements:
        dataset = db.get(Dataset, source.dataset_id)
        for day, channels in dataset_index(dataset).items():
            bucket = merged.setdefault(day, {})
            for channel, rows in channels.items():
                bucket[channel] = list(rows)
    return merged


def supplement_scope(db, batch):
    """补报实际覆盖的日期与链路。"""
    _, supplements = chain_of(db, batch)
    days, channels = set(), set()
    for source in supplements:
        dataset = db.get(Dataset, source.dataset_id)
        for day, rows in dataset_index(dataset).items():
            days.add(day)
            channels |= set(rows)
    return days, channels


def _clean(rows):
    """整理一天一条链路的日记行；同日重复且数值不一致视为需核稿。"""
    if not rows:
        return None, None
    if len(rows) > 1:
        values = {round(float(row.get('interruption_min') or 0), 4) for row in rows}
        if len(values) > 1:
            return None, '日记重复且停播分钟不一致'
    row = rows[-1]
    try:
        return {
            'day': str(row.get('day')),
            'planned_min': row.get('planned_min'),
            'aired_min': row.get('aired_min'),
            'interruption_min': float(row.get('interruption_min') or 0),
        }, None
    except (TypeError, ValueError):
        return None, '停播分钟不是数值'


# --------------------------------------------------------------------------
# 归并版本
# --------------------------------------------------------------------------

def live_build(db, base_batch_id):
    return db.scalar(select(DiaryBuild).where(
        DiaryBuild.base_batch_id == base_batch_id,
        DiaryBuild.status == 'published',
        DiaryBuild.superseded_at.is_(None),
    ).order_by(DiaryBuild.published_at.desc()).limit(1))


def groups_of(db, build_id, day=None):
    query = select(OutageGroup).where(OutageGroup.build_id == build_id)
    if day is not None:
        query = query.where(OutageGroup.day == day)
    return list(db.scalars(query.order_by(OutageGroup.day.asc(), OutageGroup.group_key.asc())))


def open_build(db, data, actor):
    """创建或恢复一个归并版本。返回 (build, created)。

    - 同批次同阈值已有未完成版本时直接续算：沿用该版本创建时锁定的接线水位，
      即“中断恢复仍用原批日记和当时接线”，重试不新增版本号；
    - 已按当前接线生效的批次拒绝新建，避免静默改口径；
    - 否则按当前接线水位开新版本。
    """
    batch = db.get(DiaryBatch, data.batch_id)
    if not batch:
        raise HTTPException(404, '日记批次不存在')
    base, _ = chain_of(db, batch)
    if batch.kind == 'supplement' and live_build(db, base.id) is None:
        raise HTTPException(409, '补报需要先有该基础批次的生效版本；请先算并切换基础批次的归并')
    threshold = float(data.threshold if data.threshold is not None else DEFAULT_THRESHOLD)
    if not 0 <= threshold < 1440:
        raise HTTPException(422, '停播判定阈值须在 0 至 1440 分钟之间')

    watermark = wiring_watermark(db)

    # 中断恢复优先：同一批次同阈值仍有未完成版本时，沿用它创建时锁定的接线水位续算。
    unfinished = db.scalar(select(DiaryBuild).where(
        DiaryBuild.batch_id == batch.id,
        DiaryBuild.threshold == threshold,
        DiaryBuild.status.in_(('draft', 'building', 'failed')),
    ).order_by(DiaryBuild.id.desc()).limit(1))
    if unfinished:
        unfinished.status = 'draft'
        unfinished.error = ''
        unfinished.created_by = actor
        db.flush()
        return unfinished, False

    existing = db.scalar(select(DiaryBuild).where(
        DiaryBuild.batch_id == batch.id,
        DiaryBuild.wiring_watermark == watermark,
        DiaryBuild.threshold == threshold,
    ).order_by(DiaryBuild.id.desc()).limit(1))
    if existing:
        if existing.status == 'published':
            raise HTTPException(409, '该批次在当前接线下已有生效版本；换接线请先登记新接线再建版本')
        # 已算齐但尚未切换：直接返回该版本，不重复计算也不新增版本。
        return existing, False

    version_no = (db.scalar(select(func.max(DiaryBuild.version_no)).where(
        DiaryBuild.base_batch_id == base.id)) or 0) + 1
    build = DiaryBuild(
        batch_id=batch.id,
        base_batch_id=base.id,
        wiring_watermark=watermark,
        threshold=threshold,
        version_no=version_no,
        status='draft',
        created_by=actor,
        created_at=time.time(),
    )
    db.add(build)
    db.flush()
    return build, True


def _same(group, payload):
    return (
        group.status == payload['status']
        and group.attribution == payload['attribution']
        and group.sources == payload['sources']
        and group.pending_reason == payload['pending_reason']
        and group.members == payload['members']
        and group.scope == payload['scope']
        and group.evidence == payload['evidence']
    )


def _copy(db, build_id, old):
    row = OutageGroup(
        build_id=build_id,
        day=old.day,
        group_key=old.group_key,
        status=old.status,
        attribution=old.attribution,
        sources=old.sources,
        pending_reason=old.pending_reason,
        note=old.note,
        members=old.members,
        scope=old.scope,
        evidence=old.evidence,
        wiring_id=old.wiring_id,
        reused=True,
        reused_from_group_id=old.id,
        created_at=time.time(),
    )
    db.add(row)
    return row


def _insert(db, build_id, payload, wiring_id):
    row = OutageGroup(
        build_id=build_id,
        day=payload['day'],
        group_key=payload['key'],
        status=payload['status'],
        attribution=payload['attribution'],
        sources=payload['sources'],
        pending_reason=payload['pending_reason'],
        note=payload['note'],
        members=payload['members'],
        scope=payload['scope'],
        evidence=payload['evidence'],
        wiring_id=wiring_id,
        reused=False,
        created_at=time.time(),
    )
    db.add(row)
    return row


def _insert_conflict(db, build_id, day, channel, reason, rows, wiring_meta, wiring_id):
    records = {str(row.get('channel')): {
        'planned_min': row.get('planned_min'),
        'aired_min': row.get('aired_min'),
        'interruption_min': row.get('interruption_min'),
    } for row in rows or []}
    row = OutageGroup(
        build_id=build_id,
        day=day,
        group_key=f'conflict:{channel}',
        status='pending',
        attribution='pending',
        sources=[],
        pending_reason=f'{channel} 在 {day} 的{reason}',
        note='同日同链路的日记重复且数值不一致，先核源稿再归并。',
        members=[],
        scope=[channel],
        evidence={'day': day, 'wiring_id': wiring_id,
                  'wiring_effective_from': (wiring_meta or {}).get('effective_from'),
                  'edges': (wiring_meta or {}).get('edges') or [],
                  'sources': (wiring_meta or {}).get('sources') or [],
                  'records': records, 'traces': {}, 'raw': rows or []},
        wiring_id=wiring_id,
        reused=False,
        created_at=time.time(),
    )
    db.add(row)
    return row


def _clear_day(db, build_id, day):
    for row in groups_of(db, build_id, day):
        db.delete(row)
    db.flush()


def _mark(db, build, day):
    days = list(build.processed_days or [])
    if day not in days:
        days.append(day)
    build.processed_days = sorted(days)
    build.progress = len(days)


def _completeness(db, build):
    """版本内每个有日记的日期都必须处理完（含无停播、无需出组的日子）。"""
    days = sorted(merged_index(db, db.get(DiaryBatch, build.batch_id)))
    done = set(build.processed_days or [])
    return len(days), [day for day in days if day not in done]


def run_build(db, build):
    """执行（或续算）一个归并版本；可安全重复调用，完成前置 ready。"""
    if build.status in ('published', 'ready'):
        return build
    build.status = 'building'
    build.error = ''
    db.flush()

    try:
        batch = db.get(DiaryBatch, build.batch_id)
        base, supplements = chain_of(db, batch)
        index = merged_index(db, batch)
        days = sorted(index)
        known = set()
        for channels in index.values():
            known |= set(channels)
        touched_days, changed = supplement_scope(db, batch)
        previous = live_build(db, base.id)

        for day in days:
            wiring = wiring_at(db, day, build.wiring_watermark)
            wiring_id = wiring.id if wiring else None
            wiring_meta = wiring_payload(wiring)

            if previous and supplements and day not in touched_days:
                old = groups_of(db, previous.id, day)
                # 只有当日接线未变化、且生效版本确实处理过这一天时才原样复用；
                # 否则（接线变更或新增日）按当日接线重算。
                covered = bool(old) or day in (previous.processed_days or [])
                if covered and all((row.wiring_id or None) == wiring_id for row in old):
                    _clear_day(db, build.id, day)
                    for row in old:
                        _copy(db, build.id, row)
                    _mark(db, build, day)
                    continue

            _clear_day(db, build.id, day)
            records, conflicts = {}, []
            for channel, rows in index.get(day, {}).items():
                row, problem = _clean(rows)
                if problem:
                    conflicts.append((channel, problem, rows))
                    continue
                if row:
                    records[channel] = row

            old_groups = groups_of(db, previous.id, day) if previous else []
            fresh = outage_logic.compute_day(
                day, wiring_meta, records,
                threshold=build.threshold, known=known, wiring_meta=wiring_meta,
            )
            for payload in fresh:
                old = next((row for row in old_groups if row.group_key == payload['key']), None)
                if old and _same(old, payload) and not outage_logic.affected(
                        {'scope': old.scope, 'sources': old.sources, 'members': old.members}, changed):
                    _copy(db, build.id, old)
                else:
                    _insert(db, build.id, payload, wiring_id)
            for channel, problem, rows in conflicts:
                _insert_conflict(db, build.id, day, channel, problem, rows, wiring_meta, wiring_id)
            _mark(db, build, day)
            db.flush()

        total, missing = _completeness(db, build)
        if missing:
            raise RuntimeError(f'{len(missing)} 天尚未算出结论，例如 {missing[0]}')
        build.progress = len(days)
        build.status = 'ready'
        build.finished_at = time.time()
        db.flush()
    except Exception as exc:
        db.rollback()
        fresh = db.get(DiaryBuild, build.id)
        if fresh:
            fresh.status = 'draft'
            fresh.error = str(exc)[:990]
            db.flush()
        raise
    return build


def publish(db, build_id):
    """一次性切换：草稿完整后公开，并让同基础批次的旧版本失效（旧版仍可照查）。"""
    build = db.get(DiaryBuild, build_id)
    if not build:
        raise HTTPException(404, '归并版本不存在')
    if build.status == 'published':
        return build
    if build.status != 'ready':
        raise HTTPException(409, '归并尚未算齐，不能切换版本')
    expected = len(merged_index(db, db.get(DiaryBatch, build.batch_id)))
    if expected and build.progress != expected:
        raise HTTPException(409, f'归并只完成 {build.progress}/{expected} 天，不能切换版本')
    now = time.time()
    current = live_build(db, build.base_batch_id)
    if current and current.id != build.id:
        db.execute(update(DiaryBuild).where(DiaryBuild.id == current.id).values(superseded_at=now))
    build.status = 'published'
    build.published_at = now
    build.superseded_at = None
    db.flush()
    return build


def build_payload(db, build):
    batch = db.get(DiaryBatch, build.batch_id)
    base = db.get(DiaryBatch, build.base_batch_id)
    groups = groups_of(db, build.id)
    return {
        'id': build.id,
        'batch_id': build.batch_id,
        'batch_label': batch.label if batch else '',
        'batch_kind': batch.kind if batch else '',
        'base_batch_id': build.base_batch_id,
        'base_label': base.label if base else '',
        'wiring_watermark': build.wiring_watermark,
        'threshold': build.threshold,
        'version_no': build.version_no,
        'status': build.status,
        'status_label': STATUS_LABEL.get(build.status, build.status),
        'error': build.error,
        'progress': build.progress,
        'created_by': build.created_by,
        'created_at': build.created_at,
        'finished_at': build.finished_at,
        'published_at': build.published_at,
        'superseded_at': build.superseded_at,
        'groups': len(groups),
        'pending_groups': sum(1 for row in groups if row.status == 'pending'),
        'reused_groups': sum(1 for row in groups if row.reused),
        'recomputed_groups': sum(1 for row in groups if not row.reused),
    }


def group_payload(row):
    return {
        'id': row.id,
        'build_id': row.build_id,
        'day': row.day,
        'group_key': row.group_key,
        'status': row.status,
        'attribution': row.attribution,
        'sources': row.sources,
        'source_label': '、'.join(item.get('node', '') for item in row.sources or []) or '待核',
        'pending_reason': row.pending_reason,
        'note': row.note,
        'members': row.members,
        'scope': row.scope,
        'evidence': row.evidence,
        'wiring_id': row.wiring_id,
        'reused': row.reused,
        'reused_from_group_id': row.reused_from_group_id,
        'created_at': row.created_at,
    }


def day_digest(db, build):
    """把版本内的组按日归拢，供值班简报按日期展开。"""
    digest = {}
    for row in groups_of(db, build.id):
        entry = digest.setdefault(row.day, {'day': row.day, 'groups': [], 'pending': 0, 'same_source': 0})
        entry['groups'].append(group_payload(row))
        if row.status == 'pending':
            entry['pending'] += 1
        if row.attribution in ('same_source', 'relay', 'multiple'):
            entry['same_source'] += 1
    return [digest[day] for day in sorted(digest)]


def history(db, base_batch_id):
    rows = db.scalars(select(DiaryBuild).where(
        DiaryBuild.base_batch_id == base_batch_id).order_by(DiaryBuild.version_no.asc()))
    return [build_payload(db, row) for row in rows]
