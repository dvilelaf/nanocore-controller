import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { ThemeToggle } from '../ThemeToggle';

const renderToggle = () =>
  render(
    <I18nextProvider i18n={i18n}>
      <ThemeToggle />
    </I18nextProvider>,
  );

beforeEach(() => {
  document.documentElement.removeAttribute('data-theme');
  window.localStorage.clear();
});

afterEach(cleanup);

describe('ThemeToggle', () => {
  it('switches to the dark theme and back, remembering the choice', () => {
    renderToggle();
    const button = screen.getByRole('button', { name: 'Dark mode' });
    expect(button).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(button);
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(window.localStorage.getItem('nanocore-theme')).toBe('dark');
    expect(button).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(button);
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem('nanocore-theme')).toBe('light');
  });

  it('starts from a saved choice', () => {
    window.localStorage.setItem('nanocore-theme', 'dark');
    renderToggle();
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(screen.getByRole('button', { name: 'Dark mode' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('ignores a corrupt saved value', () => {
    window.localStorage.setItem('nanocore-theme', 'purple');
    renderToggle();
    expect(document.documentElement.dataset.theme).not.toBe('purple');
  });

  it('still works when storage is unavailable', () => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = () => {
      throw new Error('blocked');
    };
    try {
      renderToggle();
      fireEvent.click(screen.getByRole('button', { name: 'Dark mode' }));
      expect(document.documentElement.dataset.theme).toBe('dark');
    } finally {
      Storage.prototype.setItem = original;
    }
  });
});
