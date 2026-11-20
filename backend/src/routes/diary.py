from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from src.config.database import get_db
from src.middlewares.auth import current_user, admin_only, can_write
from src.models.diary import DiaryBatch, DiaryBuild
from src.schemas.diary import BatchInput, BuildInput, WiringInput
from src.services import diary, diary_runner
from src.services.audit import record

router = APIRouter()


# ---- 接线关系 -------------------------------------------------------------

@router.get('/wiring')
def wiring_list(db=Depends(get_db), u=Depends(current_user)):
    return diary.wiring_history(db)


@router.post('/wiring', status_code=201)
def wiring_create(data: WiringInput, db=Depends(get_db), u=Depends(can_write)):
    row = diary.register_wiring(db, data, u.email)
    record(db, u.email, 'wiring.register', {
        'id': row.id, 'effective_from': row.effective_from,
        'edges': len(row.edges), 'sources': row.sources,
    })
    db.commit()
    return diary.wiring_payload(row)


# ---- 日记批次 -------------------------------------------------------------

@router.get('/diary/batches')
def batch_list(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(DiaryBatch).order_by(DiaryBatch.id.desc()))
    return [diary.batch_payload(db, row) for row in rows]


@router.post('/diary/batches', status_code=201)
def batch_create(data: BatchInput, db=Depends(get_db), u=Depends(can_write)):
    row = diary.create_batch(db, data, u.email)
    record(db, u.email, 'diary.batch', {
        'id': row.id, 'kind': row.kind, 'parent': row.parent_batch_id, 'dataset': row.dataset_id,
    })
    db.commit()
    return diary.batch_payload(db, row)


# ---- 归并版本 -------------------------------------------------------------

@router.get('/diary/builds')
def build_list(base_batch_id: int | None = None, db=Depends(get_db), u=Depends(current_user)):
    query = select(DiaryBuild).order_by(DiaryBuild.id.desc())
    if base_batch_id:
        query = query.where(DiaryBuild.base_batch_id == base_batch_id)
    return [diary.build_payload(db, row) for row in db.scalars(query.limit(100))]


@router.post('/diary/builds', status_code=201)
def build_create(data: BuildInput, db=Depends(get_db), u=Depends(can_write)):
    build, created = diary.open_build(db, data, u.email)
    record(db, u.email, 'diary.build', {
        'id': build.id, 'version': build.version_no, 'created': created,
        'watermark': build.wiring_watermark,
    })
    db.commit()
    # 初次创建放进后台；重试同一版本时也重新调度，但复用同一版本号。
    diary_runner.dispatch(build.id)
    db.expire_all()
    fresh = db.get(DiaryBuild, build.id)
    return {'build': diary.build_payload(db, fresh), 'created': created}


@router.get('/diary/builds/{id}')
def build_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    build = db.get(DiaryBuild, id)
    if not build:
        raise HTTPException(404, '归并版本不存在')
    return {
        'build': diary.build_payload(db, build),
        'days': diary.day_digest(db, build),
    }


@router.post('/diary/builds/{id}/retry')
def build_retry(id: int, db=Depends(get_db), u=Depends(can_write)):
    build = db.get(DiaryBuild, id)
    if not build:
        raise HTTPException(404, '归并版本不存在')
    if build.status == 'published':
        raise HTTPException(409, '生效中的版本不能重试')
    record(db, u.email, 'diary.retry', {'id': build.id, 'status': build.status})
    db.commit()
    diary_runner.dispatch(build.id)
    return diary.build_payload(db, build)


@router.post('/diary/builds/{id}/publish')
def build_publish(id: int, db=Depends(get_db), u=Depends(admin_only)):
    build = diary.publish(db, id)
    record(db, u.email, 'diary.publish', {
        'id': build.id, 'version': build.version_no, 'batch': build.batch_id,
    })
    db.commit()
    return diary.build_payload(db, build)


@router.get('/diary/history')
def build_history(base_batch_id: int, db=Depends(get_db), u=Depends(current_user)):
    return diary.history(db, base_batch_id)
