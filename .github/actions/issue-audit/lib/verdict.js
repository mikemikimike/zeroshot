'use strict';

const VERDICTS = ['ready', 'needs-information'];
const MAX_SUMMARY_LENGTH = 1000;
const MAX_QUESTION_LENGTH = 500;
const MAX_DEFAULT_LENGTH = 500;
const MAX_RAW_LENGTH = 64 * 1024;

class VerdictError extends Error {}

function verdictSchema() {
  return {
    type: 'object',
    additionalProperties: false,
    required: ['verdict', 'summary', 'questions'],
    properties: {
      verdict: { type: 'string', enum: VERDICTS },
      summary: { type: 'string' },
      questions: {
        type: 'array',
        items: {
          type: 'object',
          additionalProperties: false,
          required: ['question', 'suggested_default'],
          properties: {
            question: { type: 'string' },
            suggested_default: { type: 'string' },
          },
        },
      },
    },
  };
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function exactKeys(value, keys, what) {
  if (!isPlainObject(value)) throw new VerdictError(`${what} must be an object`);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new VerdictError(`${what} must have exactly the keys ${expected.join(', ')}`);
  }
}

function boundedText(value, max, what) {
  if (typeof value !== 'string') throw new VerdictError(`${what} must be a string`);
  const trimmed = value.trim();
  if (trimmed === '') throw new VerdictError(`${what} must not be empty`);
  if ([...trimmed].length > max) throw new VerdictError(`${what} exceeds ${max} characters`);
  return trimmed;
}

/** Validates an agent verdict and fails closed on anything unexpected. */
function validateVerdict(value, { maxQuestions }) {
  exactKeys(value, ['verdict', 'summary', 'questions'], 'verdict');
  if (!VERDICTS.includes(value.verdict)) {
    throw new VerdictError(`verdict must be one of ${VERDICTS.join(', ')}`);
  }
  const summary = boundedText(value.summary, MAX_SUMMARY_LENGTH, 'summary');
  if (!Array.isArray(value.questions)) throw new VerdictError('questions must be an array');
  if (value.verdict === 'ready' && value.questions.length !== 0) {
    throw new VerdictError('a ready verdict must not ask questions');
  }
  if (value.verdict === 'needs-information') {
    if (value.questions.length === 0) {
      throw new VerdictError('a needs-information verdict must ask at least one question');
    }
    if (value.questions.length > maxQuestions) {
      throw new VerdictError(`at most ${maxQuestions} questions are allowed`);
    }
  }
  const questions = value.questions.map((question, index) => {
    exactKeys(question, ['question', 'suggested_default'], `questions[${index}]`);
    return {
      question: boundedText(question.question, MAX_QUESTION_LENGTH, `questions[${index}].question`),
      suggested_default: boundedText(
        question.suggested_default,
        MAX_DEFAULT_LENGTH,
        `questions[${index}].suggested_default`
      ),
    };
  });
  return { verdict: value.verdict, summary, questions };
}

/** Parses a single JSON object from agent text: bare JSON or one fenced json block. */
function extractJson(text) {
  if (typeof text !== 'string') throw new VerdictError('agent output must be text');
  if (text.length > MAX_RAW_LENGTH) throw new VerdictError('agent output is too large');
  const trimmed = text.trim();
  let candidate = trimmed;
  if (!trimmed.startsWith('{')) {
    const fenced = /^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$/.exec(trimmed);
    if (!fenced) throw new VerdictError('agent output is not a JSON object');
    candidate = fenced[1];
  }
  try {
    return JSON.parse(candidate);
  } catch {
    throw new VerdictError('agent output is not valid JSON');
  }
}

const SECRET_PATTERNS = [
  /\bgh[pousr]_[A-Za-z0-9]{20,}/,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/,
  /\bsk-[A-Za-z0-9_-]{20,}/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
];

/** Rejects output that contains a known secret or a credential-shaped string. */
function assertNoSecrets(text, secrets = []) {
  for (const secret of secrets) {
    if (secret && secret.length >= 8 && text.includes(secret)) {
      throw new VerdictError('agent output contains a credential');
    }
  }
  if (SECRET_PATTERNS.some((pattern) => pattern.test(text))) {
    throw new VerdictError('agent output contains a credential-shaped value');
  }
}

// Characters that render invisibly or reorder text: controls, bidi controls, zero-width and
// format characters, Unicode tag characters and variation selectors.
const HIDDEN_CHARACTERS =
  // eslint-disable-next-line no-control-regex
  /[\u{0}-\u{8}\u{b}\u{c}\u{e}-\u{1f}\u{7f}-\u{9f}\u{ad}\u{34f}\u{61c}\u{115f}\u{1160}\u{17b4}\u{17b5}\u{180b}-\u{180f}\u{200b}-\u{200f}\u{202a}-\u{202e}\u{2060}-\u{206f}\u{3164}\u{fe00}-\u{fe0f}\u{feff}\u{ffa0}\u{fff0}-\u{fffb}\u{e0000}-\u{e0fff}]/gu;
const MARKDOWN_PUNCTUATION = /[\\`*_{}[\]()#+!|~<>&"=-]/g;

function stripHidden(text) {
  return String(text).replace(HIDDEN_CHARACTERS, '');
}

/**
 * Makes untrusted text safe to embed inline in a GitHub comment: one line, bounded, no hidden
 * characters, no HTML or Markdown syntax, and no @-mentions.
 */
function sanitizeInline(text, max) {
  let value = stripHidden(text).replace(/\s+/gu, ' ').trim();
  const characters = [...value];
  if (characters.length > max) value = `${characters.slice(0, max - 1).join('')}…`;
  return value.replace(/@/g, '＠').replace(MARKDOWN_PUNCTUATION, (match) => `\\${match}`);
}

module.exports = {
  MAX_DEFAULT_LENGTH,
  MAX_QUESTION_LENGTH,
  MAX_SUMMARY_LENGTH,
  VerdictError,
  assertNoSecrets,
  extractJson,
  sanitizeInline,
  stripHidden,
  validateVerdict,
  verdictSchema,
};
