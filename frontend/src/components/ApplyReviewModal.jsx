import { useState } from 'react'
import { confirmApply, cancelApply } from '../api'
import ErrorBanner from './ErrorBanner'

const PLATFORM_LABEL = {
  linkedin: 'LinkedIn',
  indeed: 'Indeed',
  generic_ats: '通用 ATS',
}

const SOURCE_LABEL = {
  profile: '来自画像',
  llm: 'AI 生成',
  manual_required: '需要你补',
  skipped_has_value: '已有内容',
}

// 验证码相关的警告单独挑出来，别跟"这个字段没填上"这种一般性提示混在一起——
// 验证码意味着自动流程真的卡住了，必须用户自己在浏览器里处理。
const CAPTCHA_WARNING_RE = /验证码/

export default function ApplyReviewModal({ job, draft, onConfirmed, onCancelled }) {
  const [confirming, setConfirming] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [actionError, setActionError] = useState(null)

  const busy = confirming || cancelling

  async function handleConfirm() {
    setConfirming(true)
    setActionError(null)
    try {
      await confirmApply(draft.session_id)
      onConfirmed()
    } catch (err) {
      setActionError(err)
    } finally {
      setConfirming(false)
    }
  }

  async function handleCancel() {
    setCancelling(true)
    try {
      await cancelApply(draft.session_id)
    } catch (err) {
      // 取消这个动作本身失败了也不该把用户卡在弹窗里出不去——记下来，还是关闭。
      // eslint-disable-next-line no-console
      console.error('取消投递会话失败：', err)
    } finally {
      setCancelling(false)
      onCancelled()
    }
  }

  const warnings = draft.warnings || []
  const captchaWarnings = warnings.filter((w) => CAPTCHA_WARNING_RE.test(w))
  const otherWarnings = warnings.filter((w) => !CAPTCHA_WARNING_RE.test(w))
  const platformLabel = PLATFORM_LABEL[draft.platform] || draft.platform

  return (
    <div className="apply-modal-overlay" onClick={busy ? undefined : onCancelled}>
      <div className="apply-modal" onClick={(e) => e.stopPropagation()}>
        <div className="apply-modal__header">
          <div>
            <h3 className="apply-modal__title">投递前确认</h3>
            <p className="apply-modal__job-title">{job.title}</p>
          </div>
          <span className="badge badge--source">{platformLabel}</span>
          <button
            type="button"
            className="apply-modal__close"
            onClick={onCancelled}
            disabled={busy}
            aria-label="关闭"
          >
            ×
          </button>
        </div>

        {draft.screenshot_url && (
          <div className="apply-modal__screenshot">
            <img src={draft.screenshot_url} alt="自动填表后的页面截图" />
          </div>
        )}

        <p className="result-box__label">填写报告（{draft.filled_fields.length} 个字段）：</p>
        <div className="apply-modal__table-wrap">
          <table className="apply-modal__table">
            <thead>
              <tr>
                <th>字段</th>
                <th>填的值</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              {draft.filled_fields.map((f, i) => (
                <tr key={i}>
                  <td>{f.label}</td>
                  <td>{f.value ? f.value : <em className="apply-modal__empty-value">（空）</em>}</td>
                  <td>
                    <span className={`badge badge--source-${f.source}`}>
                      {SOURCE_LABEL[f.source] || f.source}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {captchaWarnings.length > 0 && (
          <div className="apply-modal__captcha-alert">
            {captchaWarnings.map((w, i) => (
              <p key={i}>🛑 {w}</p>
            ))}
          </div>
        )}

        {otherWarnings.length > 0 && (
          <ul className="apply-modal__warnings">
            {otherWarnings.map((w, i) => (
              <li key={i}>⚠️ {w}</li>
            ))}
          </ul>
        )}

        {!draft.ready_to_submit && (
          <p className="apply-modal__not-ready">
            系统没找到可点击的提交按钮，可能卡在中间某一步——建议直接看浏览器窗口手动处理，不建议点"确认投递"。
          </p>
        )}

        <ErrorBanner error={actionError} onDismiss={() => setActionError(null)} />

        <div className="apply-modal__footer">
          <button type="button" className="btn-ghost" onClick={handleCancel} disabled={busy}>
            {cancelling ? '取消中...' : '取消'}
          </button>
          <button type="button" className="btn-primary" onClick={handleConfirm} disabled={busy}>
            {confirming ? '提交中...' : '确认投递'}
          </button>
        </div>
      </div>
    </div>
  )
}
