import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { ArrowLeft, ArrowRight, RotateCcw, Sunrise } from "lucide-react";
import { Link } from "react-router-dom";
import JourneySteps from "../components/JourneySteps";
import { getDailyQuote, getQuotePresentation, inspirationImage } from "../lib/inspiration";
import type { DailyQuote } from "../lib/inspiration";
import { chinaDateKey } from "../lib/quoteSchedule";

export default function InspirationPage() {
  const [date, setDate] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setDate(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  return <InspirationScene quote={getDailyQuote(date)} date={date} />;
}

export function InspirationScene({ quote, date }: { quote: DailyQuote; date: Date }) {
  const [replay, setReplay] = useState(0);
  const presentation = getQuotePresentation(quote);
  const letters = Array.from(presentation.text);
  const letterDelay = presentation.language === "en" ? 0.026 : 0.095;
  const writingDuration = 0.6 + letters.length * letterDelay;
  const words = presentation.text.split(/(\s+)/);
  const background = inspirationImage(quote.background);
  const portrait = inspirationImage(quote.portrait);
  const dateLabel = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long",
  }).format(date);

  return (
    <main className="inspiration-page" style={{ "--quote-background": background ? `url("${background}")` : "none" } as CSSProperties}>
      <div className="inspiration-scenery" aria-hidden="true" />
      {portrait && <img className="inspiration-portrait" src={portrait} alt="" aria-hidden="true" />}
      <div className="inspiration-shade" aria-hidden="true" />
      <header className="inspiration-header">
        <Link to="/welcome" className="inspiration-back"><ArrowLeft size={18} /><span>晨光打卡</span></Link>
        <time dateTime={chinaDateKey(date)}>{dateLabel}</time>
      </header>
      <section className="inspiration-content" aria-labelledby="inspiration-heading">
        <p className="eyebrow"><Sunrise size={18} /> A LITTLE LIGHT FOR TODAY</p>
        <h1 id="inspiration-heading" className="inspiration-kicker">今日一语<span>献给每一个坚持的你</span></h1>
        <div className="quote-writing" key={`${quote.id}-${replay}`}>
          <span className="quote-open" aria-hidden="true">“</span>
          <blockquote className={`daily-quote daily-quote--${presentation.font}`} lang={presentation.language} aria-label={`“${presentation.text}” —— ${quote.author}`}>
            <span aria-hidden="true">{presentation.language === "en" ? words.map((word, wordIndex) => {
              const precedingLength = words.slice(0, wordIndex).join("").length;
              if (/^\s+$/.test(word)) return <span key={wordIndex}>{word}</span>;
              return <span className="quote-word" key={wordIndex}>{Array.from(word).map((letter, index) => (
                <span className="quote-letter" key={index} style={{ "--letter-delay": `${0.35 + (precedingLength + index) * letterDelay}s` } as CSSProperties}>{letter}</span>
              ))}</span>;
            }) : letters.map((letter, index) => (
              <span className="quote-letter" key={index} style={{ "--letter-delay": `${0.35 + index * letterDelay}s` } as CSSProperties}>{letter}</span>
            ))}<span className="quote-pen" style={{ "--writing-duration": `${writingDuration + 0.2}s` } as CSSProperties} /></span>
          </blockquote>
          <span className="quote-close" aria-hidden="true" style={{ "--author-delay": `${writingDuration}s` } as CSSProperties}>”</span>
          <div className="quote-attribution" style={{ "--author-delay": `${writingDuration}s` } as CSSProperties}>
            <span className="quote-attribution__line" /><div><p>{quote.author}</p><a href={quote.sourceUrl} target="_blank" rel="noreferrer">{quote.source}</a></div>
          </div>
        </div>
        <div className="inspiration-actions">
          <Link className="journey-button journey-button--cream" to="/submit"><span>带着这束光，开始打卡</span><ArrowRight size={23} /></Link>
          <button className="quote-replay" type="button" onClick={() => setReplay((value) => value + 1)}><RotateCcw size={15} />重播书写</button>
        </div>
        {quote.original && <details className="quote-original"><summary>中文释义</summary><p>{quote.text}</p><small>{quote.translation}</small></details>}
      </section>
      <footer className="inspiration-footer"><JourneySteps current={2} light /><span>让坚持，成为习惯。</span></footer>
    </main>
  );
}
