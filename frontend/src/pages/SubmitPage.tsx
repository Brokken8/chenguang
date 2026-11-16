import SubmitForm from "../components/SubmitForm";
import { Link, Navigate } from "react-router-dom";
import { ArrowLeft, UserRound } from "lucide-react";
import UniversityBrand from "../components/UniversityBrand";
import JourneySteps from "../components/JourneySteps";
import { readIdentity } from "../lib/identity";

export default function SubmitPage() {
  const identity = readIdentity();
  if (!identity) return <Navigate to="/welcome" replace />;
  return (
    <main className="checkin-page">
      <header className="checkin-site-header"><UniversityBrand compact /><Link to="/inspiration"><ArrowLeft size={16} />今日一语</Link></header>
      <div className="checkin-container">
        <div className="checkin-welcome"><div><p className="eyebrow">MAKE TODAY COUNT</p><h1>把今天的努力，留在这里。</h1><p>一项小小的完成，也是在向前。</p></div><JourneySteps current={3} /></div>
        <div className="checkin-identity"><span><UserRound size={19} /><strong>{identity.name}</strong><span>{identity.studentId}</span></span><Link to="/welcome">更换信息 <ArrowLeft size={13} /></Link></div>
        <SubmitForm key={identity.studentId} identity={identity} />
        <footer className="checkin-footer">日有所进，晨光不负。<span>华中科技大学 · 光学与电子信息学院</span></footer>
      </div>
    </main>
  );
}
