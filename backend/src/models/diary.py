from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class WiringVersion(Base):
    """接线关系的一次登记；同一生效日可再登记新版本，以较大的 id 为准。"""

    __tablename__ = 'wiring_versions'
    id: Mapped[int] = mapped_column(primary_key=True)
    effective_from: Mapped[str] = mapped_column(String(10), index=True)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class DiaryBatch(Base):
    """停播日记批次：基础批次直接包一个数据批次；补报批次用 parent_batch_id 指向被补批次。"""

    __tablename__ = 'diary_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    kind: Mapped[str] = mapped_column(String(20), default='base')
    parent_batch_id: Mapped[int | None] = mapped_column(ForeignKey('diary_batches.id'), nullable=True)
    label: Mapped[str] = mapped_column(String(200), default='')
    checksum: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)


class DiaryBuild(Base):
    """一次归并版本。

    wiring_watermark 记录创建时的接线版本上限，中断恢复与重试都沿用“当时接线”；
    同一批次 + 同一接线水位 + 同一补报批次只允许一个版本，重试复用而不新增版本号。
    """

    __tablename__ = 'diary_builds'
    __table_args__ = (
        UniqueConstraint('batch_id', 'wiring_watermark', 'threshold', name='uq_build_scope'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('diary_batches.id'))
    base_batch_id: Mapped[int] = mapped_column(ForeignKey('diary_batches.id'), index=True)
    wiring_watermark: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[float] = mapped_column(Double, default=10.0)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default='draft')
    error: Mapped[str] = mapped_column(String(1000), default='')
    progress: Mapped[int] = mapped_column(Integer, default=0)
    processed_days: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    published_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    superseded_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class OutageGroup(Base):
    """归并后的停播组；每个版本各自留一份，旧版可照查。"""

    __tablename__ = 'outage_groups'
    __table_args__ = (UniqueConstraint('build_id', 'day', 'group_key', name='uq_group_build_day'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey('diary_builds.id'), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    group_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default='ready')
    attribution: Mapped[str] = mapped_column(String(30), default='source')
    sources: Mapped[list] = mapped_column(JSON, default=list)
    pending_reason: Mapped[str] = mapped_column(String(1000), default='')
    note: Mapped[str] = mapped_column(String(1000), default='')
    members: Mapped[list] = mapped_column(JSON, default=list)
    scope: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    wiring_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    reused_from_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
