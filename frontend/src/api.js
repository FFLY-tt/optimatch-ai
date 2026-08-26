// 统一封装所有后端接口调用。
// /api 和 /files 走 vite.config.js 里配的 proxy 转发到后端（localhost:8000），
// 这里直接用相对路径，不用写死完整 URL。

export class ApiError extends Error {
  constructor(detail, status) {
    super(detail)
    this.name = 'ApiError'
    this.detail = detail
    this.status = status
  }
}

export class NetworkError extends Error {
  constructor(message) {
    super(message)
    this.name = 'NetworkError'
  }
}

async function request(path, options = {}) {
  let res
  try {
    res = await fetch(path, options)
  } catch (err) {
    // fetch 本身抛异常，说明请求根本没打到后端（后端没起、断网这类）
    throw new NetworkError(
      `网络请求失败，请确认后端服务（uvicorn）是否已启动：${err.message}`
    )
  }

  let data = null
  try {
    data = await res.json()
  } catch {
    // 非 JSON 响应（比如后端整个挂了、代理转发到了错误页），data 保持 null
  }

  if (!res.ok) {
    // FastAPI 的 HTTPException 把报错信息放在 detail 字段里
    const detail = data && data.detail ? data.detail : `请求失败（HTTP ${res.status}）`
    throw new ApiError(detail, res.status)
  }

  return data
}

export function uploadResume(file) {
  const formData = new FormData()
  formData.append('file', file)
  return request('/api/upload-resume', { method: 'POST', body: formData })
}

export function addResumeNote(noteText) {
  return request('/api/add-resume-note', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note_text: noteText }),
  })
}

export function searchJobs({ targetRole, targetRegion, maxResults }) {
  return request('/api/search-jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_role: targetRole,
      target_region: targetRegion,
      max_results: maxResults,
    }),
  })
}

export function tailorResume({ jobDescription, userNotes, topK }) {
  return request('/api/tailor-resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      job_description: jobDescription,
      user_notes: userNotes,
      top_k: topK,
    }),
  })
}

// format: 'docx' | 'md' | 'pdf'
export function exportResume(format, { finalContent, candidateName }) {
  return request(`/api/export-resume-${format}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ final_content: finalContent, candidate_name: candidateName }),
  })
}
