import { useState } from 'react'
import { uploadResume, addResumeNote } from '../api'
import ErrorBanner from './ErrorBanner'

export default function ResumeBuilder() {
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState(null)
  const [uploadError, setUploadError] = useState(null)
  const [isDragOver, setIsDragOver] = useState(false)

  const [noteText, setNoteText] = useState('')
  const [submittingNote, setSubmittingNote] = useState(false)
  const [noteResult, setNoteResult] = useState(null)
  const [noteError, setNoteError] = useState(null)

  async function handleUpload() {
    if (!file) return
    setUploading(true)
    setUploadError(null)
    setUploadResult(null)
    try {
      const data = await uploadResume(file)
      setUploadResult(data)
    } catch (err) {
      setUploadError(err)
    } finally {
      setUploading(false)
    }
  }

  // 拖拽上传：格式校验故意不在这里做——现有"点击选文件"那条路本来就没有前端校验，
  // 选错格式直接交给 handleUpload -> /api/upload-resume 去 400，走 ErrorBanner 展示。
  // 拖拽只是换一种拿到 File 对象的方式，拿到之后完全复用同一个 setFile + handleUpload
  // 流程，不再单独写一套格式判断/报错逻辑。
  function handleDragOver(e) {
    e.preventDefault() // 不 preventDefault 的话，浏览器默认行为是把文件当新页面打开
    setIsDragOver(true)
  }

  function handleDragLeave(e) {
    e.preventDefault()
    // dragleave 在鼠标从容器移到它内部的子元素（input/button/提示文字）时也会触发，
    // 用 relatedTarget 判断一下，避免拖在区域内部移动时高亮一闪一闪。
    if (e.currentTarget.contains(e.relatedTarget)) return
    setIsDragOver(false)
  }

  function handleDrop(e) {
    e.preventDefault()
    setIsDragOver(false)
    const dropped = e.dataTransfer.files?.[0] || null
    if (dropped) setFile(dropped)
  }

  async function handleAddNote() {
    if (!noteText.trim()) return
    setSubmittingNote(true)
    setNoteError(null)
    setNoteResult(null)
    try {
      const data = await addResumeNote(noteText)
      setNoteResult(data)
      setNoteText('')
    } catch (err) {
      setNoteError(err)
    } finally {
      setSubmittingNote(false)
    }
  }

  return (
    <>
      <div className="subsection">
        <h3>上传简历</h3>
        <div
          className={`dropzone${isDragOver ? ' dropzone--active' : ''}`}
          onDragEnter={handleDragOver}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <p className="dropzone__hint">
            📄 点击选择文件，或将文件拖到这里（支持 .pdf / .md）
          </p>
          <div className="control-row">
            <input
              type="file"
              accept=".pdf,.md"
              onChange={(e) => setFile(e.target.files[0] || null)}
            />
            <button className="btn-primary" onClick={handleUpload} disabled={!file || uploading}>
              {uploading ? '上传中...' : '上传'}
            </button>
          </div>
          {file && <p className="dropzone__filename">已选择：{file.name}</p>}
        </div>
        <ErrorBanner error={uploadError} onDismiss={() => setUploadError(null)} />
        {uploadResult && (
          <div className="result-box">
            <p>
              新增 chunk：<b>{uploadResult.new_chunks}</b> ｜ 累计 chunk：
              <b>{uploadResult.total_chunks_stored}</b>
            </p>
            <p>
              新增 keyword：<b>{uploadResult.new_keywords}</b> ｜ 累计 keyword：
              <b>{uploadResult.total_keywords_stored}</b>
            </p>
            <p className="result-box__label">解析预览（前几行）：</p>
            <pre className="preview-text">{uploadResult.preview_text}</pre>
          </div>
        )}
      </div>

      <div className="subsection">
        <h3>补充自由文本</h3>
        <textarea
          rows={4}
          value={noteText}
          onChange={(e) => setNoteText(e.target.value)}
          placeholder="补充一些没写进简历的经历……"
        />
        <div className="control-row">
          <button className="btn-primary" onClick={handleAddNote} disabled={!noteText.trim() || submittingNote}>
            {submittingNote ? '提交中...' : '提交'}
          </button>
        </div>
        <ErrorBanner error={noteError} onDismiss={() => setNoteError(null)} />
        {noteResult && (
          <div className="result-box">
            <p>
              新增 chunk：<b>{noteResult.new_chunks}</b> ｜ 累计 chunk：
              <b>{noteResult.total_chunks_stored}</b>
            </p>
          </div>
        )}
      </div>
    </>
  )
}
