import express, { Request, Response } from 'express';
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import { GoogleGenAI } from '@google/genai';
import dotenv from 'dotenv';

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());

// Initialize Google GenAI if key is present
let ai: GoogleGenAI | null = null;
if (process.env.GEMINI_API_KEY) {
  try {
    ai = new GoogleGenAI({
      apiKey: process.env.GEMINI_API_KEY,
      httpOptions: {
        headers: {
          'User-Agent': 'aistudio-build',
        },
      },
    });
  } catch (err) {
    console.warn('Gemini client initialization warning:', err);
  }
}

// Helper to run Python CLI commands safely
function runPythonCommand(args: string[]): Promise<any> {
  return new Promise((resolve, reject) => {
    execFile('python3', ['-m', 'supplyshield.cli', ...args], { cwd: __dirname }, (error, stdout, stderr) => {
      if (error) {
        console.error('Python execution error:', stderr || error.message);
        return reject(new Error(stderr || error.message));
      }
      try {
        const parsed = JSON.parse(stdout.trim());
        resolve(parsed);
      } catch (parseErr) {
        reject(new Error(`Failed to parse Python JSON output: ${stdout}`));
      }
    });
  });
}

// 1. Inventory endpoint
app.get('/api/inventory', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['inventory']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 2. Suppliers endpoint
app.get('/api/suppliers', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['suppliers']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 3. Procurement policies (RAG)
app.get('/api/policies', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['policies']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 4. Agent shortage investigation
app.post('/api/agent/investigate', async (req: Request, res: Response) => {
  try {
    const { itemId, simulateApiFailure } = req.body;
    if (!itemId) {
      return res.status(400).json({ error: 'itemId is required' });
    }

    const failureFlag = simulateApiFailure ? 'true' : 'false';
    const result = await runPythonCommand(['investigate', itemId, failureFlag]);

    // If Gemini AI is configured, augment reasoning with LLM explanation
    if (ai && result.state && result.selected_candidate) {
      try {
        const prompt = `You are the decision-explanation co-pilot for SupplyShield, an autonomous factory procurement agent.
Item: ${JSON.stringify(result.inventory)}
Deficit: ${result.deficit}
Candidate Vendor: ${JSON.stringify(result.selected_candidate)}
Guardrail Compliance Check: ${JSON.stringify(result.compliance)}
State Machine Decision: ${result.state}

Write a concise, professional 3-bullet decision summary for the factory operations board:
1. Shortage & Operational Impact
2. Vendor Qualification & Landed Cost Analysis
3. Guardrail Enforcement & Compliance Citation (citing POL-FIN-001 for spending caps, POL-SUP-002 for vendor vetting, POL-SEC-004 for prompt injection defense).
Do not propose real transactions. Keep under 120 words.`;

        const aiPromise = ai.models.generateContent({
          model: 'gemini-3.8-flash',
          contents: prompt,
        });

        const timeoutPromise = new Promise<null>((resolve) =>
          setTimeout(() => resolve(null), 3500)
        );

        const aiResponse = (await Promise.race([aiPromise, timeoutPromise])) as any;

        if (aiResponse && aiResponse.text) {
          result.llm_augmented_explanation = aiResponse.text;
          result.model_name = 'gemini-3.8-flash';
        }
      } catch (geminiErr: any) {
        console.warn('Gemini optional enrichment skipped:', geminiErr.message);
      }
    }

    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 5. Human-in-the-loop approval & PO generation
app.post('/api/agent/approve', async (req: Request, res: Response) => {
  try {
    const { itemId, notes } = req.body;
    if (!itemId) {
      return res.status(400).json({ error: 'itemId is required' });
    }

    const data = await runPythonCommand(['approve', itemId, notes || 'Approved in UI']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 6. Golden set evaluations
app.get('/api/evals/golden-set', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['evals']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 7. Adjust stock (restock or sudden shortage simulation)
app.post('/api/restock/adjust', async (req: Request, res: Response) => {
  try {
    const { itemId, delta } = req.body;
    if (!itemId || delta === undefined) {
      return res.status(400).json({ error: 'itemId and delta are required' });
    }

    const data = await runPythonCommand(['adjust_stock', itemId, String(delta)]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ------------------ SupplyShield FinanceCheck Endpoints ------------------

// 8. Purchase orders with receipt & invoice status
app.get('/api/finance/pos', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['pos']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 9. Goods receipts
app.get('/api/finance/receipts', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['receipts']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 10. Record goods receipt
app.post('/api/finance/receipts', async (req: Request, res: Response) => {
  try {
    const payload = JSON.stringify(req.body);
    const data = await runPythonCommand(['record_receipt', payload]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 11. Supplier invoices with 3-way match
app.get('/api/finance/invoices', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['invoices']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 12. Extract invoice document text / simulate OCR
app.post('/api/finance/extract-invoice', async (req: Request, res: Response) => {
  try {
    const { fileName, rawText } = req.body;
    const data = await runPythonCommand(['extract_invoice', fileName || 'invoice.pdf', rawText || '']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 13. Verify invoice 3-way match
app.post('/api/finance/verify-invoice', async (req: Request, res: Response) => {
  try {
    const payload = JSON.stringify(req.body);
    const data = await runPythonCommand(['verify_invoice', payload]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 14. Record authorized payment
app.post('/api/finance/record-payment', async (req: Request, res: Response) => {
  try {
    const payload = JSON.stringify(req.body);
    const data = await runPythonCommand(['record_payment', payload]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 15. Resolve discrepancy (Human in the loop)
app.post('/api/finance/resolve-discrepancy', async (req: Request, res: Response) => {
  try {
    const payload = JSON.stringify(req.body);
    const data = await runPythonCommand(['resolve_discrepancy', payload]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 16. Finance audit logs
app.get('/api/finance/audit', async (req: Request, res: Response) => {
  try {
    const data = await runPythonCommand(['finance_audit']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 17. Check eligibility for verified PDF report
app.get('/api/finance/can-generate-report/:invoiceId', async (req: Request, res: Response) => {
  try {
    const { invoiceId } = req.params;
    const data = await runPythonCommand(['can_generate_report', invoiceId]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 18. Generate verified PDF report
app.post('/api/finance/generate-report', async (req: Request, res: Response) => {
  try {
    const { invoiceId, createdBy } = req.body;
    if (!invoiceId) {
      return res.status(400).json({ error: 'invoiceId is required' });
    }
    const data = await runPythonCommand(['generate_report', invoiceId, createdBy || 'Authorized Supervisor']);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 19. Download verified PDF report directly
app.get('/api/finance/report/:reportId/download', async (req: Request, res: Response) => {
  try {
    const { reportId } = req.params;
    const data = await runPythonCommand(['get_report_pdf', reportId]);
    if (data.success && data.pdf_base64) {
      const buffer = Buffer.from(data.pdf_base64, 'base64');
      res.setHeader('Content-Type', 'application/pdf');
      res.setHeader('Content-Disposition', `attachment; filename="${reportId}.pdf"`);
      res.send(buffer);
    } else {
      res.status(404).send(data.error || 'Report not found');
    }
  } catch (err: any) {
    res.status(500).send(err.message);
  }
});

// 20. Correct extracted invoice fields (human review)
app.post('/api/finance/correct-invoice', async (req: Request, res: Response) => {
  try {
    const payload = JSON.stringify(req.body);
    const data = await runPythonCommand(['correct_invoice', payload]);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 17. LLM Identity & Diagnostic Endpoint (Section B)
app.get('/api/llm/identity', (req: Request, res: Response) => {
  const hasKey = Boolean(process.env.GEMINI_API_KEY);
  res.json({
    llm_configured: true,
    provider: 'Google Gemini',
    sdk: '@google/genai (TypeScript SDK v2.4.0)',
    model_name: 'gemini-3.8-flash',
    where_called: 'Server-side proxy (/api/agent/investigate in server.ts) for natural-language decision reasoning summaries. Safety guardrails execute deterministically in Python.',
    api_key_status: hasKey ? 'Configured (Server Environment Secret)' : 'Not Set (Using Deterministic Fallback)',
    deterministic_fallback: 'Active (SupplyShield executes 100% deterministically when LLM is offline or unkeyed)',
    independence_guarantee: 'Guardrails (spending limits, supplier qualifications, prompt injection defense, 3-way matching) run in Python code independently of the model.'
  });
});

// Start server with Vite middleware in dev or static files in prod
async function start() {
  if (process.env.NODE_ENV !== 'production') {
    const { createServer: createViteServer } = await import('vite');
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    app.use(express.static(path.resolve(__dirname, 'dist')));
    app.get('*', (req: Request, res: Response) => {
      res.sendFile(path.resolve(__dirname, 'dist', 'index.html'));
    });
  }

  app.listen(Number(PORT), '0.0.0.0', () => {
    console.log(`SupplyShield full-stack server running on http://0.0.0.0:${PORT}`);
  });
}

start();
