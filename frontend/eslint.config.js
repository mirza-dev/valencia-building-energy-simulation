import js from '@eslint/js'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'playwright-report/**', 'test-results/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        document: 'readonly', window: 'readonly', localStorage: 'readonly',
        fetch: 'readonly', FormData: 'readonly', File: 'readonly', RequestInit: 'readonly',
        EventSource: 'readonly', URLSearchParams: 'readonly', console: 'readonly',
        setTimeout: 'readonly', clearTimeout: 'readonly', process: 'readonly',
      },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
)
