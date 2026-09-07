import {
  CTA,
  Compare,
  Features,
  Footer,
  Hero,
  HowItWorks,
  Nav,
  Trust,
} from "../components/landing/Sections";

export function Landing() {
  return (
    <>
      <Nav />
      <main>
        <Hero />
        <HowItWorks />
        <Features />
        <Compare />
        <Trust />
        <CTA />
      </main>
      <Footer />
    </>
  );
}
