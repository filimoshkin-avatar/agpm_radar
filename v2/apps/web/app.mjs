export const applicationIdentity = Object.freeze({
  application: "radar-v2-web",
  stage: "stage-2-skeleton",
  status: "skeleton",
});

const root = document.querySelector("#app");

if (!(root instanceof HTMLElement)) {
  throw new Error("Radar V2 application root is missing");
}

const heading = document.createElement("h1");
heading.textContent = "Radar V2";

const status = document.createElement("p");
status.textContent = "Stage 2 isolated application skeleton";

root.replaceChildren(heading, status);
