import 'vuetify/styles'
import { createVuetify } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi'

// Commonplace book palette — gold on parchment-dark.
// Replaces the earlier Void Space/GitHub-blue scheme. Every Vuetify
// component inherits these so even un-redesigned surfaces (Galaxy /
// Graph canvas chrome, dialogs, system menus) feel like one book.
export default createVuetify({
  theme: {
    defaultTheme: 'dark',
    themes: {
      dark: {
        dark: true,
        colors: {
          background: '#0e0b06',
          surface: '#14110a',
          'surface-bright': '#1c1810',
          'surface-variant': '#22180e',
          'on-surface': '#eee0c4',
          'on-background': '#eee0c4',
          primary: '#c8a96e',          // gold
          'primary-darken-1': '#a88a52',
          secondary: '#9d6c4a',         // burnt sienna
          'secondary-darken-1': '#7a5239',
          success: '#8aa96e',           // sage gold
          error: '#c47a6a',             // muted vermillion
          warning: '#c89e6e',           // amber-gold
          info: '#6e8fa9',              // dusk blue
          accent: '#c8a96e',
        },
        variables: {
          'border-color': '#c8a96e',
          'border-opacity': 0.18,
          'high-emphasis-opacity': 0.94,
          'medium-emphasis-opacity': 0.55,
          'disabled-opacity': 0.3,
          'hover-opacity': 0.05,
          'focus-opacity': 0.08,
          'activated-opacity': 0.1,
          'pressed-opacity': 0.14,
        },
      },
    },
  },
  defaults: {
    VCard: {
      rounded: 0,           // no rounded corners — flat folio pages
      variant: 'flat',
      color: 'surface',
    },
    VBtn: {
      rounded: 0,
      variant: 'flat',
    },
    VChip: {
      rounded: 0,
    },
    VTextField: {
      variant: 'underlined', // bottom rule only — folio input style
      density: 'compact',
      hideDetails: true,
    },
    VTextarea: {
      variant: 'outlined',
      density: 'compact',
      rounded: 0,
      hideDetails: true,
    },
    VSelect: {
      variant: 'underlined',
      density: 'compact',
      hideDetails: true,
    },
    VTable: {
      density: 'comfortable',
    },
    VDialog: {
      // dialogs render as folio pages
      transition: 'fade-transition',
    },
  },
  icons: {
    defaultSet: 'mdi',
    aliases,
    sets: { mdi },
  },
})
