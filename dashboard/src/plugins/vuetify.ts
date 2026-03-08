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
          background: '#0a0e14',
          surface: '#131820',
          'surface-bright': '#1a2130',
          'surface-variant': '#1e2736',
          'on-surface': '#e6edf3',
          'on-background': '#e6edf3',
          primary: '#58a6ff',
          'primary-darken-1': '#388bfd',
          secondary: '#bc8cff',
          'secondary-darken-1': '#a371f7',
          success: '#3fb950',
          error: '#f85149',
          warning: '#d29922',
          info: '#58a6ff',
        },
        variables: {
          'border-color': '#ffffff',
          'border-opacity': 0.06,
          'high-emphasis-opacity': 0.95,
          'medium-emphasis-opacity': 0.55,
          'disabled-opacity': 0.3,
          'hover-opacity': 0.06,
          'focus-opacity': 0.1,
          'activated-opacity': 0.1,
          'pressed-opacity': 0.14,
        },
      },
    },
  },
  defaults: {
    VCard: {
      rounded: 'lg',
      variant: 'flat',
      color: 'surface',
    },
    VBtn: {
      rounded: 'lg',
      variant: 'flat',
    },
    VChip: {
      rounded: 'lg',
    },
    VTextField: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'lg',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VTextarea: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'lg',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VSelect: {
      variant: 'solo-filled',
      density: 'compact',
      rounded: 'lg',
      flat: true,
      bgColor: 'surface-bright',
      hideDetails: true,
    },
    VTable: {
      density: 'comfortable',
    },
  },
  icons: {
    defaultSet: 'mdi',
    aliases,
    sets: { mdi },
  },
})
