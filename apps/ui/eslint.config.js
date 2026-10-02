import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // `const { [id]: _drop, ...rest } = prev` のような意図的な破棄を許す（接頭辞 _ の規約）
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
      // 日本語 UI なので全角スペース（U+3000）はテンプレートリテラル・JSX テキスト内で許す
      'no-irregular-whitespace': ['error', { skipTemplates: true, skipJSXText: true }],
      // HMR の作法の話であって不具合ではないため警告に留める（定数の同居を許す）
      'react-refresh/only-export-components': 'warn',
    },
  },
])
