/** 停播归并的展示口径；与后端 outage_logic.NOTE / attribution 保持一致。 */
export const Attribution = {
  relay: '正常中继截断',
  same_source: '可能同源',
  multiple: '多疑似源头',
  pending: '待核',
  source: '链路自身',
};

export const AttributionTone = {
  relay: 'calm',
  same_source: 'warn',
  multiple: 'warn',
  pending: 'alert',
  source: 'plain',
};

export const BuildStatus = {
  draft: '待计算',
  building: '计算中',
  ready: '待切换',
  published: '生效中',
  failed: '计算失败',
};

export const Basis = { diary: '播控日记', signal: '信号源', missing: '缺报' };

export const State = { outage: '停播', ok: '正常', missing: '缺报', signal: '信号源', cycle: '成环' };
