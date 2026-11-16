const STEPS = ["遇见晨光", "今日一语", "开启打卡"];

export default function JourneySteps({ current, light = false }: { current: number; light?: boolean }) {
  return (
    <ol className={`journey-steps${light ? " journey-steps--light" : ""}`} aria-label="打卡流程">
      {STEPS.map((label, index) => (
        <li key={label} className={current === index + 1 ? "is-current" : current > index + 1 ? "is-complete" : ""}
          aria-current={current === index + 1 ? "step" : undefined}>
          <span className="journey-steps__number">0{index + 1}</span>
          <span>{label}</span>
        </li>
      ))}
    </ol>
  );
}
