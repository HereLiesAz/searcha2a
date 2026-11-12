/** Public, owner-editable metadata. Never place tokens or private endpoints here. */
export const agent = {
  name: 'My Agent',
  description: 'A text agent running on infrastructure controlled by its owner.',
  version: '1.0.0',
  skills: [{
    id: 'text-chat',
    name: 'Text conversation',
    description: 'Responds synchronously to text messages.',
    tags: ['text', 'conversation'],
  }],
};
