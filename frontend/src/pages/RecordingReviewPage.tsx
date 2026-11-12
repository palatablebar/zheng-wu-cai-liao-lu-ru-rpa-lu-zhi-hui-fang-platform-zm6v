import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { processApi } from '../api/processes';
import EmptyState from '../components/common/EmptyState';
import Panel from '../components/common/Panel';
import { useAsync } from '../hooks/useAsync';
import { useAuthStore } from '../stores/auth';
import {
  DIFF_TYPE_LABELS, PROCESS_VERSION_STATUS_LABELS, RECORDING_STATUS_LABELS,
  STEP_ACTION_LABELS,
} from '../constants/enums';
import type { RecordingDiffItem, RecordingImport } from '../types';

const DIFF_TONE: Record<string, string> = {
  suspected_misalign: 'danger',
  added: 'ok',
  missing: 'warn',
  updated: 'warn',
  unchanged: 'idle',
};

export default function RecordingReviewPage() {
  const { recordingId } = useParams();
  const navigate = useNavigate();
  const user = useAuthStore((state) => state.user);
  const canReview = user?.role === 'admin' || user?.role === 'engineer';
  const { data, loading, error, reload } = useAsync(
    () => processApi.recordingDetail(Number(recordingId)), [recordingId]);
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  const [note, setNote] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);

  const recording: RecordingImport | undefined = data?.recording;
  const summary = recording?.diff_summary;
  const misalignItems = useMemo(
    () => (recording?.diff_items || []).filter((item) => item.type === 'suspected_misalign'),
    [recording],
  );
  const checkedMisalign = useMemo(
    () => misalignItems.filter((item) => item.seq !== null && checked[item.seq]).map((item) => item.seq as number),
    [misalignItems, checked],
  );
  const allMisalignConfirmed = misalignItems.length > 0
    && checkedMisalign.length === misalignItems.length
    && recording?.status === 'pending_review';

  if (loading) return <div className="page"><div className="panel-body muted">加载录制候选…</div></div>;
  if (error || !recording) {
    return (
      <div className="page">
        <div className="banner">{error || '录制批次不存在'}</div>
        <button className="btn" type="button" style={{ marginTop: 14 }}
          onClick={() => navigate(-1)}>返回</button>
      </div>
    );
  }

  const toggle = (item: RecordingDiffItem) => {
    if (item.seq === null || recording.status !== 'pending_review') return;
    setChecked((prev) => ({ ...prev, [item.seq as number]: !prev[item.seq as number] }));
  };

  const confirm = async () => {
    setBusy(true);
    setMessage('');
    try {
      const result = await processApi.confirmRecording(recording.id, checkedMisalign, note);
      setMessage(result.message || '已确认，生成待发布版本');
      reload();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '确认失败');
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    setBusy(true);
    setMessage('');
    try {
      await processApi.rejectRecording(recording.id, note || '审阅不通过');
      setMessage('已驳回该录制候选，正式运行不受影响');
      reload();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : '驳回失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="crumb">
            <button type="button" className="btn btn-ghost btn-sm"
              onClick={() => navigate(`/processes/${recording.process_id}/edit`)}
              style={{ marginRight: 8 }}>← 步骤编辑器</button>
            录制导入 / 候选审阅
          </div>
          <h1 className="page-title">录制批次 #{recording.id}</h1>
          <p className="page-sub mono">
            状态 {RECORDING_STATUS_LABELS[recording.status] || recording.status} ·
            指纹 {recording.content_fingerprint.slice(0, 12)}… ·
            导入人 {recording.created_by || '—'}
          </p>
        </div>
        <div className="head-meta">
          {summary ? (
            <>
              <span className="chip danger">疑似错位 {summary.suspected_misalign}</span>
              <span className="chip ok">新增 {summary.added}</span>
              <span className="chip warn">缺失 {summary.missing}</span>
              <span className="chip">更新 {summary.updated}</span>
              <span className="chip">未变 {summary.unchanged}</span>
            </>
          ) : null}
        </div>
      </header>

      {message ? <div className="banner warn" style={{ marginBottom: 14 }}>{message}</div> : null}

      {summary?.requires_manual_confirmation && recording.status === 'pending_review' ? (
        <div className="banner" style={{ marginBottom: 14 }}>
          检测到 {summary.suspected_misalign} 个疑似错位字段（如「法人姓名」被定位到相邻的「经办人姓名」）。
          稳健分无法识别这类语义错位，必须逐条人工勾选确认后才能生成待发布版本；
          确认并发布前，正式运行仍使用原发布版本。
        </div>
      ) : null}

      <Panel kicker="FIELD DIFF · 按字段名称 / 动作 / 样例值"
        title="录制与当前生效版本的差异清单" tight>
        {recording.diff_items.length === 0 ? (
          <EmptyState title="没有差异项" hint="录制内容与生效版本完全一致。" />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="diff-table">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>分类</th>
                  <th style={{ width: 60 }}>序号</th>
                  <th>字段名称（录制 → 生效版本）</th>
                  <th style={{ width: 90 }}>动作</th>
                  <th>样例值</th>
                  <th>定位器（录制）</th>
                  <th style={{ width: 200 }}>判定依据 / 人工确认</th>
                </tr>
              </thead>
              <tbody>
                {recording.diff_items.map((item, index) => (
                  <tr key={`${item.type}-${item.seq ?? 'b'}-${index}`}
                    className={item.type === 'suspected_misalign' ? 'row-danger' : ''}>
                    <td>
                      <span className={`chip ${DIFF_TONE[item.type] || ''}`}>
                        {DIFF_TYPE_LABELS[item.type] || item.type}
                      </span>
                    </td>
                    <td className="mono">{item.seq ?? `原${item.baseline_seq ?? ''}`}</td>
                    <td>
                      <div>{item.field_name || '—'}</div>
                      {item.baseline_field_name && item.baseline_field_name !== item.field_name ? (
                        <div className="tiny muted">原：{item.baseline_field_name}</div>
                      ) : null}
                    </td>
                    <td>
                      {item.action
                        ? STEP_ACTION_LABELS[item.action as keyof typeof STEP_ACTION_LABELS] || item.action
                        : STEP_ACTION_LABELS[item.baseline_action as keyof typeof STEP_ACTION_LABELS] || '—'}
                    </td>
                    <td className="mono tiny">{item.sample_value || '—'}</td>
                    <td className="mono tiny" style={{ overflowWrap: 'anywhere' }}>
                      {item.selector || item.baseline_selector || '—'}
                    </td>
                    <td>
                      <div className="tiny muted">{item.reasons.join('；')}</div>
                      {item.type === 'suspected_misalign' && recording.status === 'pending_review' ? (
                        <label className="flex align-center gap-8" style={{ marginTop: 6 }}>
                          <input type="checkbox"
                            checked={Boolean(item.seq !== null && checked[item.seq])}
                            onChange={() => toggle(item)} />
                          <span className="tiny">已核对页面，确认该定位无误</span>
                        </label>
                      ) : null}
                      {item.review_status === 'misalign_confirmed' ? (
                        <span className="chip ok" style={{ marginTop: 6 }}>人工已确认 · {item.confirmed_by || ''}</span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {recording.status === 'pending_review' ? (
        <Panel kicker="REVIEW DECISION" title="审阅结论" tight>
          <label className="field">
            <span className="field-label">审阅备注（可选，随批次留痕）</span>
            <input className="input" value={note} onChange={(event) => setNote(event.target.value)}
              placeholder="如：已对照改版后页面逐字段核对" />
          </label>
          {!canReview ? (
            <div className="tiny faint">审计只读角色：候选确认/驳回需要值守操作员或管理员权限。</div>
          ) : (
            <div className="flex gap-8" style={{ marginTop: 10 }}>
              <button className="btn btn-primary btn-sm" type="button" onClick={confirm}
                disabled={busy || !allMisalignConfirmed}>
                {busy ? '提交中…' : '确认候选并生成待发布版本'}
              </button>
              <button className="btn btn-danger btn-sm" type="button" onClick={reject} disabled={busy}>
                驳回本次录制
              </button>
              {!allMisalignConfirmed && misalignItems.length > 0 ? (
                <span className="tiny faint">
                  还有 {misalignItems.length - checkedMisalign.length} 个疑似错位未逐条确认
                </span>
              ) : null}
            </div>
          )}
        </Panel>
      ) : (
        <Panel kicker="RESULT" title="批次结论" tight>
          <div className="flex gap-12 align-center" style={{ flexWrap: 'wrap' }}>
            <span className="chip">{RECORDING_STATUS_LABELS[recording.status]}</span>
            {recording.version_id ? (
              <span className="chip warn">
                关联待发布版本 #{recording.version_id}，请到步骤编辑器执行「发布新版本」完成整条流程切换
              </span>
            ) : null}
            {recording.reviewed_by ? (
              <span className="tiny muted">审阅人 {recording.reviewed_by}</span>
            ) : null}
          </div>
        </Panel>
      )}

      <div className="tiny faint" style={{ marginTop: 12 }}>
        版本说明：候选确认只生成 {PROCESS_VERSION_STATUS_LABELS.pending_publish} 快照；
        发布是整条流程一次原子切换，已启动的运行继续引用启动时的版本快照。
      </div>
    </div>
  );
}
