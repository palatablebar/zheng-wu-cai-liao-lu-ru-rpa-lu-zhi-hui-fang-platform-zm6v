import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { processApi } from '../../api/processes';
import type { ProcessItem } from '../../types';

interface Props {
  process: ProcessItem;
  onClose: () => void;
}

const EXAMPLE = JSON.stringify([
  {
    action: 'input',
    target_desc: '录入法人姓名',
    sample_value: '张法定',
    locator_candidates: [
      { strategy: 'css', selector: "#change-form .field-agent input:nth-child(2)", desc: '经办人姓名输入框（相邻字段）' },
    ],
  },
], null, 2);

export default function RecordingImportDialog({ process, onClose }: Props) {
  const navigate = useNavigate();
  const [text, setText] = useState('');
  const [replace, setReplace] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setError('');
    let steps: Record<string, unknown>[];
    try {
      const parsed = JSON.parse(text);
      if (!Array.isArray(parsed)) throw new Error('录制 JSON 顶层必须是步骤数组');
      steps = parsed;
    } catch (err) {
      setError(`JSON 解析失败：${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setLoading(true);
    try {
      const result = await processApi.importRecording(process.id, steps, replace);
      if (result.deduplicated) {
        navigate(`/processes/recordings/${result.recording.id}`);
        return;
      }
      navigate(`/processes/recordings/${result.recording.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <div className="panel modal">
        <header className="panel-head">
          <div>
            <div className="panel-kicker">RECORDING IMPORT · 候选化导入</div>
            <div className="panel-title">{process.name}</div>
          </div>
          <button className="btn btn-ghost btn-sm right" type="button" onClick={onClose}>关闭</button>
        </header>
        <div className="modal-body stack">
          <div className="banner warn">
            导入不会直接修改流程步骤：系统会按字段名称、动作、样例值与当前生效版本比对，
            生成含「新增 / 缺失 / 疑似错位」的可审阅候选；疑似错位人工确认并发布前，正式运行仍用原版本。
          </div>
          <label className="field">
            <span className="field-label">录制步骤 JSON（支持 sample_value 样例值字段）</span>
            <textarea className="input mono" rows={12} value={text}
              placeholder={EXAMPLE}
              onChange={(event) => setText(event.target.value)} />
          </label>
          <label className="flex align-center gap-8">
            <input type="checkbox" checked={replace} onChange={(event) => setReplace(event.target.checked)} />
            <span className="tiny">replace 模式（整段替换语义，仍需审阅确认后才生效）</span>
          </label>
          {error ? <div className="banner">{error}</div> : null}
        </div>
        <div className="modal-foot">
          <button className="btn btn-ghost" type="button" onClick={onClose}>取消</button>
          <button className="btn btn-primary" type="button" onClick={submit} disabled={loading || !text.trim()}>
            {loading ? '分析中…' : '生成审阅候选'}
          </button>
        </div>
      </div>
    </div>
  );
}
