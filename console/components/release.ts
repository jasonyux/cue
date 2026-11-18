// The release is the built console: Timeline v2 alone, with no theme switch and no debug pages. The dev server keeps
// every theme and page, for debugging.
export const release = () => process.env.NODE_ENV === "production";
