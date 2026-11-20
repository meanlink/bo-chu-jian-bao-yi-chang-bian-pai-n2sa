"""归并任务的后台执行入口。

默认在同一进程的线程里执行，便于单容器演示；测试可注入同步执行器，
以便断言“算齐后才切换”的行为。
"""
import logging
import threading

from src.config.database import Session
from src.models import DiaryBuild
from src.services import diary

_lock = threading.Lock()
_pending = set()


def _work(build_id):
    with _lock:
        if build_id in _pending:
            return
        _pending.add(build_id)
    try:
        with Session() as db:
            build = db.get(DiaryBuild, build_id)
            if not build:
                return
            try:
                diary.run_build(db, build)
                db.commit()
            except Exception:
                # run_build 已回滚并记下错误，这里保留草稿以便中断恢复后重试。
                db.rollback()
                logging.exception('Diary build failed: %s', build_id)
    finally:
        with _lock:
            _pending.discard(build_id)


def dispatch(build_id, background=True):
    if not background:
        _work(build_id)
        return
    threading.Thread(target=_work, args=(build_id,), daemon=True).start()
