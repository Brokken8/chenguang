export default function UniversityBrand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`university-brand${compact ? " university-brand--compact" : ""}`}>
      <div className="university-brand__school">
        <img className="school-brand-image" src="/brand/hust-official.png" alt="华中科技大学校徽与校名" />
      </div>
      <span className="university-brand__divider" aria-hidden="true" />
      <div className="university-brand__college">
        <span className="college-emblem">
          <img src="/brand/oei-official.png" alt="光学与电子信息学院院徽" />
        </span>
        <span className="college-wordmark">
          <strong>光学与电子信息学院</strong>
          <span>SCHOOL OF OPTICAL AND ELECTRONIC INFORMATION</span>
        </span>
      </div>
    </div>
  );
}
