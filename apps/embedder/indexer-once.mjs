import { buildEmbedSummaryMessage } from "../../shared/src/messages.js";
import { indexChangedDocuments } from "../../shared/src/embedding-service.js";
import { validateRetrievalConfig } from "../../shared/config/index.js";

try {
  validateRetrievalConfig();
  const summary = await indexChangedDocuments({ logger: (...args) => console.log("[embedder]", ...args) });
  console.log("[embedder]", buildEmbedSummaryMessage(summary));
  console.log(`__SUMMARY_JSON__${JSON.stringify(summary)}`);
  process.exit(0);
} catch (error) {
  console.error(`[embedder] indexing run failed: ${error.message}`);
  process.exit(1);
}
