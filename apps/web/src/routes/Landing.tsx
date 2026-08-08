import {
  CTA,
  Features,
  Footer,
  Hero,
  HowItWorks,
  Nav,
  Observability,
} from "../components/landing/Sections";

export function Landing() {
  return (
    <>
      <Nav />
      <main>
        <Hero />
        <Observability />
        <Features />
        <HowItWorks />
        <CTA />
      </main>
      <Footer />
    </>
  );
}
