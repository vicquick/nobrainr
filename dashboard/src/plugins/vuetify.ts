import 'vuetify/styles'
import { createVuetify } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi'

export default createVuetify({
  theme: {
    defaultTheme: 'dark',
    themes: {
      dark: {
        dark: true,
        colors: {
          background: '#0d1117',
          surface: '#161b22',
          primary: '#58a6ff',
          secondary: '#bc8cff',
          success: '#3fb950',
          error: '#f85149',
          warning: '#d29922',
          info: '#58a6ff',
        },
      },
    },
  },
  icons: {
    defaultSet: 'mdi',
    aliases,
    sets: { mdi },
  },
})
