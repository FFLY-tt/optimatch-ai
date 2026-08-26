import { useState } from 'react'
import { uploadResume, addResumeNote } from '../api'
import ErrorBanner from './ErrorBanner'

export default function ResumeBuilder() {
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState(null)
  const [uploadError, setUploadError] = useState(null)

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
