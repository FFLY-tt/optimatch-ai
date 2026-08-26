import { useState, useRef } from 'react'
import ResumeBuilder from './components/ResumeBuilder'
import JobSearch from './components/JobSearch'
import TailorResume from './components/TailorResume'
import './App.css'

export default function App() {
  // job_description 提到这一层，让 JobSearch 的"用这条生成简历"按钮
  // 能直接把内容填进 TailorResume——两个组件本来是兄弟关系，不用引入
  // 状态管理库，提到共同父组件、用 props 往下传就够了。
  const [jobDescription, setJobDescription] = useState('')
  const tailorSectionRef = useRef(null)

  function handleUseJobForTailor(content) {
    setJobDescription(content)
    tailorSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="app">
      <h1>OptiMatch AI — 联调面板</h1>

      <div className="mega-frame">
        <section className="stage" data-accent="mint">
          <div className="stage__badge">01</div>
          <div className="stage__header">
            <h2 className="stage__title">画像构建</h2>
            <p className="stage__subtitle">STAGE_01 · BUILD_PROFILE</p>
          </div>
          <div className="stage__card">
            <ResumeBuilder />
          </div>
        </section>

        <section className="stage" data-accent="lavender">
          <div className="stage__badge">02</div>
          <div className="stage__header">
            <h2 className="stage__title">职位搜索</h2>
            <p className="stage__subtitle">STAGE_02 · SEARCH_JOBS</p>
          </div>
          <div className="stage__card">
            <JobSearch onUseForTailor={handleUseJobForTailor} />
          </div>
        </section>

        <section className="stage" data-accent="straw" ref={tailorSectionRef}>
          <div className="stage__badge">03</div>
          <div className="stage__header">
            <h2 className="stage__title">定制简历生成与导出</h2>
            <p className="stage__subtitle">STAGE_03 · TAILOR_AND_EXPORT</p>
          </div>
          <div className="stage__card">
            <TailorResume jobDescription={jobDescription} onJobDescriptionChange={setJobDescription} />
          </div>
        </section>
      </div>
    </div>
  )
}
