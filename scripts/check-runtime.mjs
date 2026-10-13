if (Number(process.versions.node.split(".")[0]) < 22) {
  throw new Error("Node.js 22 or later is required.");
}
