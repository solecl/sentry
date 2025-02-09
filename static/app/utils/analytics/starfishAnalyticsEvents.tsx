export type StarfishEventParameters = {
  'starfish.chart.zoom': {
    end: number;
    start: number;
    route?: string;
  };
  'starfish.pageview': {
    route: string;
  };
  'starfish.panel.open': {};
  'starfish.request': {
    duration: number;
    statusCode?: string;
  };
  'starfish.samples.loaded': {
    count: number;
  };
  'starfish.web_service_view.endpoint_list.endpoint.clicked': {
    endpoint: string;
  };
  'starfish.web_service_view.endpoint_list.header.clicked': {
    direction: string;
    header: string;
  };
  'starfish.web_service_view.endpoint_list.search': {
    query: string;
  };
};

export type StarfishEventKey = keyof StarfishEventParameters;

export const starfishEventMap: Record<keyof StarfishEventParameters, string> = {
  'starfish.chart.zoom': 'Starfish: Chart Zoomed',
  'starfish.pageview': 'Starfish: Page Viewed',
  'starfish.panel.open': 'Starfish: Slide Over Panel Opened',
  'starfish.request': 'Starfish: API Request Completed',
  'starfish.samples.loaded': 'Starfish: Samples Loaded',
  'starfish.web_service_view.endpoint_list.endpoint.clicked':
    'Starfish: Web Service View Endpoint List Endpoint Clicked',
  'starfish.web_service_view.endpoint_list.header.clicked':
    'Starfish: Web Service View Endpoint List Header Clicked',
  'starfish.web_service_view.endpoint_list.search':
    'Starfish: Web Service View Endpoint List Search',
};
