import { useState } from "react";
import { ArrowRight, Check, Sunrise } from "lucide-react";
import { useNavigate } from "react-router-dom";
import UniversityBrand from "../components/UniversityBrand";
import JourneySteps from "../components/JourneySteps";
import { readRememberedIdentity, saveIdentity, STUDENT_ID_PATTERN } from "../lib/identity";

export default function WelcomePage() {
  const [remembered] = useState(readRememberedIdentity);
  const [name, setName] = useState(remembered.name);
  const [studentId, setStudentId] = useState(remembered.studentId);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const validId = STUDENT_ID_PATTERN.test(studentId);
  const canEnter = Boolean(name.trim()) && validId;

  function enter(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTouched(true);
    if (!canEnter) return;
    try {
      saveIdentity({ name: name.trim(), studentId });
      navigate("/inspiration");
    } catch {
      setError("浏览器暂时无法保存本次信息，请允许此站点使用会话存储后重试。");
    }
  }

  return (
    <main className="welcome-page">
      <header className="welcome-header"><UniversityBrand /></header>
      <div className="welcome-layout">
        <section className="welcome-intro">
          <p className="eyebrow"><span /> EVERY MORNING, A NEW BEGINNING</p>
          <div className="morning-title-wrap">
            <span className="morning-title-orbit" aria-hidden="true" />
            <h1 className="morning-title">晨光<span>打卡</span></h1>
            <span className="morning-seal" aria-hidden="true">日有所进</span>
          </div>
          <p className="welcome-tagline">让每一次坚持，<br />都有光的回响。</p>
          <p className="welcome-description">从清晨的一小步开始，<br />把学习与运动，慢慢变成生活的习惯。</p>
          <div className="welcome-horizon" aria-hidden="true"><Sunrise size={34} strokeWidth={1} /><span>向光而行 · 日日有新</span></div>
        </section>

        <section className="identity-card" aria-labelledby="identity-heading">
          <div className="identity-card__topline"><span>开启今天</span><span>01 / 03</span></div>
          <h2 id="identity-heading">很高兴，今天也遇见你。</h2>
          <form onSubmit={enter} noValidate>
            <label className="identity-field" htmlFor="checkin-name"><span>你的姓名 <small>NAME</small></span>
              <input id="checkin-name" name="name" autoComplete="name" placeholder="请输入姓名" maxLength={50}
                value={name} required aria-invalid={touched && !name.trim()} onChange={(event) => setName(event.target.value)} />
            </label>
            <label className="identity-field" htmlFor="checkin-student-id"><span>你的学号 <small>STUDENT ID</small></span>
              <div className="identity-input-wrap">
                <input id="checkin-student-id" name="studentId" autoCapitalize="characters" autoComplete="off" spellCheck={false}
                  placeholder="请输入学号" value={studentId} maxLength={10} pattern="U202[0-9]{6}" required
                  aria-invalid={touched && !validId}
                  onBlur={() => setTouched(true)} onChange={(event) => setStudentId(event.target.value.toUpperCase().trim())} />
                {validId && <Check size={18} className="identity-valid" aria-label="学号格式正确" />}
              </div>
            </label>
            {touched && !name.trim() && <p className="identity-error" role="alert">请填写姓名。</p>}
            {error && <p className="identity-error" role="alert">{error}</p>}
            <div className={`entry-action${canEnter ? " entry-action--ready" : ""}`}>
              {canEnter ? <button className="journey-button" type="submit"><span>向晨光出发</span><ArrowRight size={23} /></button>
                : <p className="entry-action__hint">填写姓名和有效学号后，即可开启 <ArrowRight size={17} /></p>}
            </div>
          </form>
          <div className="identity-card__foot"><span className="tiny-sun" aria-hidden="true" />每一个认真开始的今天，都值得被记录。</div>
        </section>
      </div>
      <footer className="welcome-footer"><JourneySteps current={1} /><span>华中科技大学 · 光学与电子信息学院</span></footer>
    </main>
  );
}
