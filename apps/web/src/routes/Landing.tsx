import {
  CTA,
  Features,
  Footer,
  Hero,
  HowItWorks,
  Nav,
  Observability,
  Stats,
} from "../components/landing/Sections";

export function Landing() {
  return (
    <>
      <Nav />
      <main>
        <Hero />
        <Stats />
        <Observability />
        <Features />
        <HowItWorks />
        <CTA />
      </main>
      <Footer />
    </>
  );
}
