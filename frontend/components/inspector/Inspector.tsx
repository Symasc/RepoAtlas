"use client";

import { Button, CloseButton, Tabs } from "@mantine/core";
import { ArrowRightIcon } from "@phosphor-icons/react";
import type { ReactNode } from "react";

import classes from "./Inspector.module.css";

export interface InspectorTab {
  value: string;
  label: string;
  /** A count beside the label, when the tab lists things. */
  count?: number | null;
  panel: ReactNode;
}

/**
 * The docked panel beside the canvas. The canvas shows where things are and
 * what touches what; this is where the detail is read: lists, source, evidence.
 */
export function Inspector({
  label,
  icon,
  title,
  subtitle,
  actions,
  tabs,
  tab,
  onTab,
  onClose,
}: {
  label: string;
  icon: ReactNode;
  title: string;
  subtitle?: ReactNode;
  /** The primary action, such as opening what is inspected. */
  actions?: ReactNode;
  tabs: InspectorTab[];
  tab: string;
  onTab: (value: string) => void;
  onClose?: () => void;
}) {
  const current = tabs.some((t) => t.value === tab) ? tab : tabs[0]?.value;
  return (
    <aside className={classes.inspector} aria-label={label}>
      <header className={classes.header}>
        <span className={classes.icon}>{icon}</span>
        <div className={classes.heading}>
          <h2 className={classes.title} title={title}>
            {title}
          </h2>
          {subtitle && <p className={classes.subtitle}>{subtitle}</p>}
        </div>
        {onClose && <CloseButton size="md" onClick={onClose} aria-label="Close the inspector" className={classes.close} />}
      </header>
      {actions && <div className={classes.actions}>{actions}</div>}
      <Tabs
        value={current}
        onChange={(value) => value && onTab(value)}
        keepMounted={false}
        classNames={{ root: classes.tabs, list: classes.tabList, tab: classes.tab, panel: classes.panel }}
      >
        <Tabs.List>
          {tabs.map((t) => (
            <Tabs.Tab key={t.value} value={t.value}>
              {t.label}
              {t.count != null && <span className={classes.tabCount}>{t.count}</span>}
            </Tabs.Tab>
          ))}
        </Tabs.List>
        {tabs.map((t) => (
          <Tabs.Panel key={t.value} value={t.value}>
            {t.panel}
          </Tabs.Panel>
        ))}
      </Tabs>
    </aside>
  );
}

/** A quiet line in a panel that has nothing to list. */
export function Quiet({ children }: { children: ReactNode }) {
  return <p className={classes.quiet}>{children}</p>;
}

/**
 * The panel's one way in. Filled in the accent's deeper shade in both themes:
 * the dark theme's own accent leaves white text at about 3:1.
 */
export function OpenButton({ name, onOpen }: { name: string; onOpen: () => void }) {
  return (
    <Button size="xs" color="atlas.7" rightSection={<ArrowRightIcon size={13} />} onClick={onOpen}>
      Open {name}
    </Button>
  );
}
