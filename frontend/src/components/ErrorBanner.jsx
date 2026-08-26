// 通用报错提示条——网络层错误和后端 400 业务报错分别用不同底色，
// 但都走这一个组件展示，不用 alert()。
export default function ErrorBanner({ error, onDismiss }) {
  if (!error) return null

  const isNetwork = error.name === 'NetworkError'
  const text = error.detail || error.message

  return (
    <div className={`error-banner ${isNetwork ? 'error-banner--network' : 'error-banner--api'}`}>
      <span className="error-banner__icon">{isNetwork ? '⚠️' : '❌'}</span>
      <span className="error-banner__text">{text}</span>
      {onDismiss && (
        <button className="error-banner__dismiss" onClick={onDismiss} aria-label="关闭">
          ×
        </button>
      )}
    </div>
  )
}
