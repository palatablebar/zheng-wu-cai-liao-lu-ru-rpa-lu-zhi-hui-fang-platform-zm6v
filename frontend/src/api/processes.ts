import { http } from './client';
import type {
  LocatorCandidate,
  ProcessItem,
  ProcessVersionInfo,
  RecordingImport,
  RecordingImportResult,
} from '../types';

export const processApi = {
  list: () => http.get<{ total: number; items: ProcessItem[] }>('/processes'),
  detail: (id: number) => http.get<ProcessItem>(`/processes/${id}`),
  updateStep: (processId: number, stepId: number, payload: Record<string, unknown>) =>
    http.put<{ step: unknown; score: unknown }>(`/processes/${processId}/steps/${stepId}`, payload),
  rescore: (processId: number) =>
    http.post<{ process_id: number; high_risk_steps: number[] }>(`/processes/${processId}/rescore`),
  publish: (processId: number, note = '') =>
    http.post<{ process: ProcessItem; version: ProcessVersionInfo | null; note: string; switched: boolean }>(
      `/processes/${processId}/publish`, { note }),
  rejectPending: (processId: number, note = '') =>
    http.post<{ process: ProcessItem; version: ProcessVersionInfo; note: string }>(
      `/processes/${processId}/reject-pending`, { note }),
  archive: (processId: number, note = '') =>
    http.post<{ process: ProcessItem; note: string }>(`/processes/${processId}/archive`, { note }),
  dryRun: (processId: number, materialId: number) =>
    http.post<{ id: number; run_code: string }>(`/processes/${processId}/dry-run`, {
      process_id: processId,
      material_id: materialId,
      trigger: 'dry_run',
    }),
  // 录制导入：返回可审阅候选（新增/缺失/疑似错位），不再直接改步骤
  importRecording: (processId: number, steps: Record<string, unknown>[], replace = false) =>
    http.post<RecordingImportResult>(`/processes/${processId}/import`, { steps, replace }),
  listRecordings: (processId: number, status?: string) =>
    http.get<{ total: number; items: RecordingImport[] }>(
      `/processes/${processId}/recordings${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  recordingDetail: (recordingId: number) =>
    http.get<{ recording: RecordingImport }>(`/processes/recordings/${recordingId}`),
  confirmRecording: (recordingId: number, confirmedMisalignSeqs: number[], note = '') =>
    http.post<RecordingImportResult>(`/processes/recordings/${recordingId}/confirm`, {
      confirmed_misalign_seqs: confirmedMisalignSeqs,
      note,
    }),
  rejectRecording: (recordingId: number, note = '') =>
    http.post<{ recording: RecordingImport }>(`/processes/recordings/${recordingId}/reject`, { note }),
};

export interface StepUpdatePayload {
  action?: string;
  target_desc?: string;
  locator_candidates?: LocatorCandidate[];
  timeout_ms?: number;
  max_retries?: number;
  assert_rule?: string;
  depends_on?: number[];
}
